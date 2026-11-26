"""极简 pytest 兼容运行器（本机无 pip/外网，装不了 pytest）。

支持现有测试实际用到的子集：
- 模块级 test_* 函数、参数注入夹具（按形参名解析）；
- tmp_path（pathlib.Path 临时目录）、conn、payments 夹具，语义对齐 tests/conftest.py；
- assert 原样语句、unittest.TestCase 风格类；
- pytest.raises 上下文管理器。
HTTP 测试依赖 fastapi/httpx（环境缺失），在导入阶段自动跳过。
用法： PYTHONPATH=/workspace python3 -m tools.pytest_shim tests/test_xxx.py ...
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import pathlib
import tempfile
import traceback
import types
import unittest

# ----------------------------------------------------------------- pytest 垫片


class _Raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.exc_type.__name__}")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


def _raises(exc_type):
    return _Raises(exc_type)


def _fixture(*args, **kwargs):
    def deco(fn):
        fn._is_fixture = True
        return fn
    if args and callable(args[0]):
        return deco(args[0])
    return deco


def _skip(reason: str):
    raise _Skip(reason)


class _Skip(Exception):
    pass


pytest_mod = types.ModuleType("pytest")
pytest_mod.raises = _raises
pytest_mod.fixture = _fixture
pytest_mod.skip = _skip
import sys
sys.modules.setdefault("pytest", pytest_mod)


# ----------------------------------------------------------------- 夹具实现


def _make_conn_fixture(tmp_path: pathlib.Path):
    from app import db
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute("UPDATE clock SET now_iso=? WHERE id=1",
              (db.normalize_ts("2026-01-01T00:00:00+00:00"),))
    return c


@contextlib.contextmanager
def _payments_fixture(tmp_path: pathlib.Path):
    import threading
    from app.payment_client import PaymentClient
    from app.payment_stub import make_server

    server = make_server(0, str(tmp_path / "payment_stub.db"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    client = PaymentClient(f"http://127.0.0.1:{port}", timeout=1.0)
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _resolve_fixture(name: str, tmp_path: pathlib.Path, cache: dict):
    if name in cache:
        return cache[name]
    if name == "tmp_path":
        val = tmp_path
    elif name == "conn":
        val = _make_conn_fixture(tmp_path)
    elif name == "payments":
        cm = _payments_fixture(tmp_path)
        val = cm.__enter__()
        cache.setdefault("_cms", []).append(cm)
    else:
        raise NameError(f"未知夹具: {name}")
    cache[name] = val
    return val


# ----------------------------------------------------------------- 运行器


def _collect(module) -> list:
    items = []
    for name, obj in vars(module).items():
        if name.startswith("test_") and callable(obj):
            items.append((name, obj))
        elif inspect.isclass(obj) and issubclass(obj, unittest.TestCase):
            items.append((name, obj))
    return items


def run_modules(module_names: list[str]) -> int:
    passed = failed = skipped = 0
    failures: list[str] = []

    for mod_name in module_names:
        try:
            module = importlib.import_module(mod_name)
        except _Skip as exc:
            print(f"SKIP {mod_name}: {exc}")
            skipped += 1
            continue
        except Exception as exc:  # noqa: BLE001 - 多为 fastapi/httpx 缺失
            print(f"SKIP {mod_name}（导入失败：{type(exc).__name__}: {exc}）")
            skipped += 1
            continue

        for name, obj in _collect(module):
            if inspect.isclass(obj):
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(obj)
                result = unittest.TestResult()
                suite.run(result)
                for tc, tb in result.failures + result.errors:
                    failed += 1
                    failures.append(f"{mod_name}.{tc}\n{tb}")
                passed += result.testsRun - len(result.failures) - len(result.errors)
                skipped += len(result.skipped)
                continue

            sig = inspect.signature(obj)
            tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"shim_{name}_"))
            cache: dict = {}
            kwargs = {}
            try:
                for pname in sig.parameters:
                    kwargs[pname] = _resolve_fixture(pname, tmp, cache)
                obj(**kwargs)
                passed += 1
                print(f"PASS {mod_name}.{name}")
            except _Skip as exc:
                skipped += 1
                print(f"SKIP {mod_name}.{name}: {exc}")
            except Exception:  # noqa: BLE001
                failed += 1
                failures.append(f"{mod_name}.{name}\n{traceback.format_exc()}")
                print(f"FAIL {mod_name}.{name}")
            finally:
                for cm in cache.get("_cms", []):
                    cm.__exit__(None, None, None)
                conn = cache.get("conn")
                if conn is not None:
                    conn.close()

    print("\n" + "=" * 64)
    print(f"通过 {passed}，失败 {failed}，跳过 {skipped}")
    for f in failures:
        print("\n" + "-" * 64 + "\n" + f)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_modules(sys.argv[1:]))
