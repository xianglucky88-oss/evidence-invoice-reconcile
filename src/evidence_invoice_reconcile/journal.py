"""仅追加审计账本（append-only audit journal）。

存储布局（``state_dir`` 下）::

    events.ndjson   仅追加事件流，每条事件含前一条事件的哈希，形成哈希链
    state.json      由事件流重放得到的物化视图（可随时重建，本身不是真相源）
    journal.lock    跨进程文件锁（fcntl.flock）
    wal-<tx>.ndjson 事务预写日志，提交时原子并入主事件流

不可变原则：

- 事件只追加、不修改、不删除。新结论通过 ``CONCLUSION_SUPERSEDE`` 事件
  携带 ``supersedes`` 指向旧结论；旧结论永久保留。
- 已确认（verified + booked）结论对同一输入哈希重跑返回同一 conclusion_id，
  不会静默改写。
- 事件哈希链在重放时逐链验证，任何篡改都会抛出 :class:`JournalTampered`。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .hashing import canonical_hash, sha256_text

EVENTS_FILE = "events.ndjson"
STATE_FILE = "state.json"
LOCK_FILE = "journal.lock"

# 事件类型
EV_RUN_START = "RUN_START"
EV_RUN_ABORT = "RUN_ABORT"
EV_CONCLUSION = "CONCLUSION_ISSUED"
EV_SUPERSEDE = "CONCLUSION_SUPERSEDE"
EV_BOOKING = "LEDGER_BOOKING"
EV_BOOKING_REJECT = "LEDGER_REJECTED"
EV_RUN_COMMIT = "RUN_COMMIT"


class JournalTampered(RuntimeError):
    """哈希链校验失败：事件流被篡改或损坏。"""


class JournalError(RuntimeError):
    pass


def _empty_state() -> Dict[str, Any]:
    return {
        "runs": {},                 # run_id -> run 元数据
        "conclusions": {},          # conclusion_id -> 结论记录
        "invoice_latest": {},       # invoice_number -> 当前 conclusion_id
        "invoice_history": {},      # invoice_number -> [conclusion_id, ...]
        "ledger": {},               # invoice_number -> 已入账结论（不可变）
        "booking_rejections": {},   # invoice_number -> [记录]
        "input_hash_index": {},     # (invoice_number, input_hash) -> conclusion_id
        "last_event_hash": None,
        "event_count": 0,
    }


def _apply_event(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    etype = event["type"]
    payload = event["payload"]
    if etype == EV_RUN_START:
        state["runs"][payload["run_id"]] = {
            "run_id": payload["run_id"],
            "started_at": event["recorded_at"],
            "status": "open",
            "tx": payload.get("tx"),
            "input_hash": payload.get("input_hash"),
        }
    elif etype == EV_RUN_ABORT:
        run = state["runs"].get(payload["run_id"])
        if run is None:
            # 崩溃恢复场景：RUN_START 只存在于被隔离的 WAL 中，
            # 主流从未物化过该 run，这里补建一条 aborted 记录
            run = {"run_id": payload["run_id"], "started_at": event["recorded_at"],
                   "status": "aborted", "tx": payload.get("tx")}
            state["runs"][payload["run_id"]] = run
        run["status"] = "aborted"
        run["abort_reason"] = payload.get("reason")
    elif etype in (EV_CONCLUSION, EV_SUPERSEDE):
        cid = payload["conclusion_id"]
        state["conclusions"][cid] = payload
        number = payload["invoice_number"]
        state["invoice_history"].setdefault(number, [])
        if cid not in state["invoice_history"][number]:
            state["invoice_history"][number].append(cid)
        state["invoice_latest"][number] = cid
        state["input_hash_index"][f"{number}|{payload['input_hash']}"] = cid
        run = state["runs"].get(payload.get("run_id"))
        if run:
            run.setdefault("conclusions", []).append(cid)
    elif etype == EV_BOOKING:
        state["ledger"][payload["invoice_number"]] = {
            "conclusion_id": payload["conclusion_id"],
            "run_id": payload["run_id"],
            "booked_at": event["recorded_at"],
            "amounts": payload["amounts"],
            "allocations": payload.get("allocations", []),
        }
    elif etype == EV_BOOKING_REJECT:
        state["booking_rejections"].setdefault(payload["invoice_number"], []).append(payload)
    elif etype == EV_RUN_COMMIT:
        run = state["runs"].get(payload["run_id"])
        if run:
            run["status"] = "committed"
            run["committed_at"] = event["recorded_at"]
    # 未知事件类型不影响状态物化（前向兼容），但仍参与哈希链校验


class AuditJournal:
    """文件型（默认）或内存型（``state_dir=None``）审计账本。"""

    def __init__(self, state_dir: Optional[str] = None, *, clock=None):
        self.state_dir = state_dir
        # 默认时钟：进程内单调递增的逻辑序号，保证事件时间戳确定性、
        # 可重放且无浮点序列化歧义；业务时间（认证/入账/付款）来自输入字段。
        self._clock = clock or _LogicalClock()
        self._mem_events: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._flock = None
        if state_dir is not None:
            os.makedirs(state_dir, exist_ok=True)
            self._events_path = os.path.join(state_dir, EVENTS_FILE)
            self._lock_path = os.path.join(state_dir, LOCK_FILE)
        else:
            self._events_path = None
            self._lock_path = None

    # ------------------------------------------------------------- 锁与恢复

    def __enter__(self):
        self._lock.acquire()
        if self.state_dir is not None:
            import fcntl
            self._flock = open(self._lock_path, "w")
            fcntl.flock(self._flock.fileno(), fcntl.LOCK_EX)
            self._recover_wal()
        return self

    def __exit__(self, *exc):
        if self._flock is not None:
            import fcntl
            fcntl.flock(self._flock.fileno(), fcntl.LOCK_UN)
            self._flock.close()
            self._flock = None
        self._lock.release()

    def _recover_wal(self) -> None:
        """崩溃恢复：残留 WAL 说明事务未提交，记入 ABORT 并隔离 WAL 文件。"""
        for name in sorted(os.listdir(self.state_dir)):
            if not name.startswith("wal-") or not name.endswith(".ndjson"):
                continue
            wal_path = os.path.join(self.state_dir, name)
            tx = name[4:-7]
            quarantine = os.path.join(self.state_dir, f"aborted-{name}")
            try:
                with open(wal_path, "r", encoding="utf-8") as fh:
                    lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                if lines:
                    first = json.loads(lines[0])
                    run_id = first.get("payload", {}).get("run_id", "unknown")
                    self._append(EV_RUN_ABORT, {
                        "run_id": run_id, "tx": tx,
                        "reason": "wal_recovered_after_crash",
                        "wal_events": len(lines),
                    })
            finally:
                os.replace(wal_path, quarantine)

    # ------------------------------------------------------------- 事件追加

    def _last_hash(self) -> Optional[str]:
        if self.state_dir is not None:
            if not os.path.exists(self._events_path) or os.path.getsize(self._events_path) == 0:
                return None
            # 反向按行读取，避免固定字节截断切断多字节 UTF-8 字符
            with open(self._events_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                block = min(size, 8192)
                fh.seek(size - block)
                data = fh.read(block)
            last_line = data.decode("utf-8").splitlines()[-1]
            return json.loads(last_line)["event_hash"]
        return self._mem_events[-1]["event_hash"] if self._mem_events else None

    def _append(self, etype: str, payload: Dict[str, Any], *, wal=None) -> Dict[str, Any]:
        # 快照隔离：事件一旦构造，payload 必须与调用方对象脱钩，
        # 后续业务回写（如 booked 标记）不得改变已落账事件的哈希输入。
        payload = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        event = {
            "event_id": uuid.uuid4().hex,
            "type": etype,
            "recorded_at": _iso(self._clock()),
            "payload": payload,
            "prev_hash": self._last_hash() if wal is None else wal["prev_hash"],
        }
        event["event_hash"] = self._event_hash(event)
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if wal is not None:
            with open(wal["path"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            wal["prev_hash"] = event["event_hash"]
        elif self.state_dir is not None:
            with open(self._events_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        else:
            self._mem_events.append(event)
        return event

    @staticmethod
    def _event_hash(event: Dict[str, Any]) -> str:
        """事件哈希只覆盖 5 个业务字段，event_hash 自身不参与。"""
        return sha256_text(
            json.dumps({k: event[k] for k in
                        ("event_id", "type", "recorded_at", "payload", "prev_hash")},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    # ------------------------------------------------------------- 事务边界

    @contextmanager
    def transaction(self, run_id: str, tx: str, input_hash: str):
        """阶段事务：WAL 预写 → 业务事件 → 提交时原子并入主事件流。

        上下文内任何异常都会回滚（WAL 隔离 + RUN_ABORT），主事件流不留半截事务。
        """
        if self.state_dir is None:
            # 内存模式：直接追加，异常由调用方决定是否 abort
            wal = None
            writer = _TxWriter(self, wal=None, run_id=run_id, tx=tx, input_hash=input_hash)
            writer.append(EV_RUN_START, {"run_id": run_id, "tx": tx, "input_hash": input_hash})
            yield writer
            return

        wal_path = os.path.join(self.state_dir, f"wal-{tx}.ndjson")
        if os.path.exists(wal_path):
            raise JournalError(f"WAL 已存在，事务标识冲突: {tx}")
        wal = {"path": wal_path, "prev_hash": self._last_hash()}
        writer = _TxWriter(self, wal=wal, run_id=run_id, tx=tx, input_hash=input_hash)
        writer.append(EV_RUN_START, {"run_id": run_id, "tx": tx, "input_hash": input_hash})
        try:
            yield writer
        except BaseException:
            # 回滚：WAL 绝不并入主流，记录中止事件
            quarantine = os.path.join(self.state_dir, f"aborted-wal-{tx}.ndjson")
            if os.path.exists(wal_path):
                os.replace(wal_path, quarantine)
            self._append(EV_RUN_ABORT, {"run_id": run_id, "tx": tx,
                                        "reason": "exception_rollback"})
            raise
        else:
            self._commit_wal(wal_path)

    def _commit_wal(self, wal_path: str) -> None:
        with open(wal_path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        # 提交：逐行重接哈希链后原子追加，随后删除 WAL。
        # 链尾必须在内存中维护——主流文件此刻仍在写入缓冲，
        # 不能反复从磁盘读取末尾，否则每条事件都会拿到同一个 prev_hash。
        chain_tip = self._last_hash()
        with open(self._events_path, "a", encoding="utf-8") as out:
            for line in lines:
                event = json.loads(line)
                event["prev_hash"] = chain_tip
                event["event_hash"] = self._event_hash(event)
                out.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                chain_tip = event["event_hash"]
            out.flush()
            os.fsync(out.fileno())
        os.remove(wal_path)

    # ------------------------------------------------------------- 重放与查询

    def events(self) -> List[Dict[str, Any]]:
        if self.state_dir is not None:
            if not os.path.exists(self._events_path):
                return []
            with open(self._events_path, "r", encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        return list(self._mem_events)

    def replay(self) -> Dict[str, Any]:
        """重放事件流并逐链验证哈希；同时物化 state.json。"""
        state = _empty_state()
        prev_hash = None
        count = 0
        for event in self.events():
            if event["prev_hash"] != prev_hash:
                raise JournalTampered(f"事件 {event['event_id']} 前序哈希断裂")
            expected = AuditJournal._event_hash(event)
            if expected != event["event_hash"]:
                raise JournalTampered(f"事件 {event['event_id']} 内容哈希不符")
            _apply_event(state, event)
            prev_hash = event["event_hash"]
            count += 1
        state["last_event_hash"] = prev_hash
        state["event_count"] = count
        if self.state_dir is not None:
            self._materialize(state)
        return state

    def _materialize(self, state: Dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True, indent=2)
            os.replace(tmp, os.path.join(self.state_dir, STATE_FILE))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def find_conclusion(self, invoice_number: str, input_hash: str,
                        state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        state = state or self.replay()
        return state["input_hash_index"].get(f"{invoice_number}|{input_hash}")

    def ledger_entry(self, invoice_number: str,
                     state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        state = state or self.replay()
        return state["ledger"].get(invoice_number)

    def latest_conclusion(self, invoice_number: str,
                          state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        state = state or self.replay()
        cid = state["invoice_latest"].get(invoice_number)
        return state["conclusions"].get(cid) if cid else None

    def run_id(self, prefix: str = "RUN") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _TxWriter:
    def __init__(self, journal: AuditJournal, *, wal, run_id: str, tx: str, input_hash: str):
        self._journal = journal
        self._wal = wal
        self.run_id = run_id
        self.tx = tx
        self.input_hash = input_hash

    def append(self, etype: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._journal._append(etype, payload, wal=self._wal)

    def commit_marker(self) -> None:
        self.append(EV_RUN_COMMIT, {"run_id": self.run_id, "tx": self.tx})


class _LogicalClock:
    """进程级单调逻辑时钟：跨所有 journal 实例单调递增，
    保证同一进程内多次 run() 的事件时间标签与序号永不重复。"""

    _counter = 0
    _lock = threading.Lock()

    def __init__(self, epoch: int = 0):
        self._base = epoch  # 注入自定义起点时仅用于该实例

    def __call__(self) -> int:
        if self._base:
            self._base += 1
            return self._base
        with _LogicalClock._lock:
            _LogicalClock._counter += 1
            return _LogicalClock._counter


def _iso(epoch) -> str:
    # 逻辑时钟直接渲染为带序号的 UTC 风格时间标签；
    # 若注入真实 time.time（浮点），回退到毫秒 ISO-8601。
    if isinstance(epoch, int):
        return f"1970-01-01T00:00:{epoch % 60:02d}.{epoch:06d}Z"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int((epoch % 1) * 1000):03d}Z"


def new_run_id(prefix: str = "RUN") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def fingerprint_payload(payload: Any) -> str:
    return canonical_hash(payload)
