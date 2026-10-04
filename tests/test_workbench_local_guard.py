"""本机工作台的来源判据（甲方验收 2026-10-05 的第 1 条 BLOCKER）。

旧形状是 `same_origin(Origin, 请求自带 Host)` —— 看着像同源校验，其实什么也没挡：
DNS rebinding 把一个外域解析到 127.0.0.1 之后，浏览器发来的 Host 与 Origin 天生一致，
于是别的网页可以替这个本机端口提交**要花额度**的任务（/submit、/credentials）。
现在的判据是"启动时真的绑上的那个回环端口"，与请求自带的那一份无关。

这一组全走真 HTTP（AGENTS.md 地雷 28：函数级调用看不见"连接被掐断"这种失败形状）。
"""
import http.client
import threading

import pytest

import tools.workbench as wb


class _Runner:
    """只给 ctx.runner 占位：这些用例一条都不该走到执行面。"""

    def running(self):  # 地雷 30：真类是方法，替身也必须是方法
        return False


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    (cfg / "config").mkdir(parents=True)
    monkeypatch.setattr(wb, "ROOT", cfg)
    ctx = wb.Workbench(config_dir="config", runner=_Runner(), real_roles=False,
                       db_rel="./runtime_scheduler/queue.db")
    httpd = wb.serve(ctx, 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _call(port, method, path, host=None, origin=None, body=None):
    """连的是 127.0.0.1，但 Host 头想说谁就说谁 —— 这正是 rebinding 的现场形状。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, text


def test_合法回环请求照常能读(srv):
    """首屏会 303 到任务那一格（地雷 35 的后续），这里只判"没被守卫拒"。
    判据要和上一条成对：把守卫整个删掉，本条仍过；把 Host 判据写反，本条必红。"""
    for host in (f"127.0.0.1:{srv}", f"localhost:{srv}", f"[::1]:{srv}"):
        status, text = _call(srv, "GET", "/", host=host)
        assert status != 400, f"本机 {host} 被来源判据拒了：{text[:140]}"
    status, text = _call(srv, "GET", "/", host="mao-rebind.example")
    assert status == 400, f"伪造 Host 竟然通过：{status} {text[:140]}"
    assert "不是本机回环地址" in text, text[:200]


def test_外域提交的表单被拒(srv):
    """这是最要紧的一条：花钱的那两个入口不能由别的网页驱动。"""
    for path in ("/submit", "/credentials"):
        status, text = _call(srv, "POST", path, host=f"127.0.0.1:{srv}",
                             origin="http://evil.example",
                             body="goal=x&workspace=")
        assert status == 403, f"{path} 被跨站表单提交驱动了：{status} {text[:140]}"


def test_同源表单能过守卫到业务层(srv):
    """判据要能区分"拒了"和"放行了"，否则把守卫整个删掉也一样"绿"。

    打一个不存在的 path：过了守卫才会落到 404，所以 404 在这里是"放行"的证据，
    同时保证不会往队列库里写任何东西。
    """
    status, _ = _call(srv, "POST", "/no-such-route", host=f"localhost:{srv}",
                      origin=f"http://localhost:{srv}", body="x=1")
    assert status == 404, f"同源请求没有走到业务层：{status}"


def test_不带origin的提交仍然被拒(srv):
    status, _ = _call(srv, "POST", "/submit", host=f"127.0.0.1:{srv}",
                      body="goal=x&workspace=")
    assert status == 403


def test_超大body被拒而不是照着取满(srv):
    """旧实现 `int(Content-Length)` 之后直接 read(那么多) —— 一句 curl 决定进程吃多少内存。"""
    big = "goal=" + ("a" * (wb.MAX_BODY_BYTES + 64))
    status, text = _call(srv, "POST", "/submit", host=f"127.0.0.1:{srv}",
                         origin=f"http://127.0.0.1:{srv}", body=big)
    assert status == 413, f"超过上限的 body 没被拒：{status} {text[:120]}"
    assert "上限" in text


def test_坏content_length不成尾异常而是回话(srv):
    """旧实现 `int("abc")` 抛在 do_POST 之外 —— 地雷 28：线程一炸，浏览器什么都看不到。"""
    conn = http.client.HTTPConnection("127.0.0.1", srv, timeout=5)
    conn.putrequest("POST", "/submit")
    conn.putheader("Host", f"127.0.0.1:{srv}")
    conn.putheader("Origin", f"http://127.0.0.1:{srv}")
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    try:
        status = conn.getresponse().status
    except (http.client.HTTPException, OSError) as exc:  # 连接被掐断 = 旧形状
        pytest.fail(f"坏 Content-Length 让这条连接死了而不是得到一句回话：{exc}")
    assert status != 500, f"服务端把自己炸了：{status}"
    # 服务还活着：同一条判据要在"线程死了"的形状上变红
    again, _ = _call(srv, "GET", "/", host=f"127.0.0.1:{srv}")
    assert again != 400, "守卫之后的正常请求应该仍然能过"


def test_端口0也要认下真正绑上的那个端口(srv):
    """测试与"端口被占"都按 0 起，判据不能用请求里的 0 —— 那会连本机都过不去。"""
    assert wb.host_problem(f"127.0.0.1:{srv}", srv) == ""
    assert "不是本机回环地址" in wb.host_problem(f"127.0.0.1:{srv - 1}", srv)
    assert wb.host_problem("", srv) == "缺少 Host 头"


def test_三种回环写法都算本机(srv):
    for h in (f"127.0.0.1:{srv}", f"localhost:{srv}", f"[::1]:{srv}"):
        assert wb.host_problem(h, srv) == "", h
    for h in (f"0.0.0.0:{srv}", f"10.0.0.8:{srv}", "mao-rebind.example", f"{srv}"):
        assert wb.host_problem(h, srv), h
