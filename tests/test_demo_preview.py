"""demo_preview 的守卫：预览图是给人核对 demo 的，取不到必须说取不到。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import demo_preview as dp


@pytest.fixture()
def site(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    (tmp_path / "about.html").write_text("<h1>about</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "leak.html").write_text("<h1>x</h1>", encoding="utf-8")
    return tmp_path


class TestDiscovery:
    def test_no_browser_is_said_not_guessed(self, monkeypatch, site, tmp_path):
        monkeypatch.setattr(dp, "BROWSER_CANDIDATES", ())
        out = dp.preview(site, tmp_path / "shots")
        assert out["status"] == "no-browser"
        assert "index.html" in out["reason"]
        assert out["shots"] == []

    def test_a_workspace_without_pages_is_not_an_error(self, tmp_path):
        out = dp.preview(tmp_path / "empty", tmp_path / "shots")
        assert out["status"] == "no-pages" and out["shots"] == []
        assert not (tmp_path / "shots").exists()

    def test_pages_are_absolute_even_when_the_workspace_is_relative(self, site,
                                                                   monkeypatch):
        """回归：批次里传的是相对路径时，`Path.as_uri()` 会当场抛。

        页面路径要变成 file:// URI，输出要变成 chrome 的 --screenshot 参数，
        两者都不该依赖调用方站在哪个目录里。
        """
        monkeypatch.chdir(site.parent)
        pages = dp.html_pages(site.name)
        assert pages and all(p.is_absolute() for p in pages)

    def test_tests_dir_never_becomes_a_preview(self, site):
        names = [p.name for p in dp.html_pages(site)]
        assert names == ["about.html", "index.html"]


class TestCommand:
    def test_headless_command_is_the_no_depend_version(self, tmp_path):
        cmd = dp.shot_command("chrome.exe", tmp_path / "index.html",
                              tmp_path / "o.png", 1280, 1600,
                              tmp_path / "profile")
        assert cmd[0] == "chrome.exe"
        assert "--headless=new" in cmd
        assert f"--screenshot={tmp_path / 'o.png'}" in cmd
        assert "--window-size=1280,1600" in cmd
        assert cmd[-1].startswith("file:///")
        # 每条命令自己的 profile 目录：共用一个会互相抢锁，第二张静默不出图
        assert f"--user-data-dir={tmp_path / 'profile'}" in cmd

    def test_render_lines_names_the_missing_one_instead_of_printing_blank(self):
        lines = dp.render_lines({"status": "ok", "shots": [
            {"page": "a.html", "png": "/x/a.png", "exit_code": 0},
            {"page": "b.html", "png": "", "exit_code": 1}]})
        assert any("/x/a.png" in ln for ln in lines)
        assert any("b.html 没出图" in ln for ln in lines)


@pytest.mark.skipif(not dp.browser_path(), reason="本机没有浏览器")
class TestRealShot:
    def test_a_real_page_actually_produces_a_png(self, site, tmp_path):
        out = dp.preview(site, tmp_path / "shots", height=400)
        assert out["status"] == "ok", out
        for shot in out["shots"]:
            png = Path(shot["png"])
            assert png.is_file() and png.stat().st_size > 1000, shot
        assert len(out["shots"]) == 2
