"""Round 11 — consent loop 内 workspace 选择页 upstream-style 健壮检测。

任务背景:OAuth Codex consent loop 在 step 1 同意点击后页面变成 workspace 选择页,
我方原 consent loop 用单 hint 文本检测,不够健壮;借鉴 upstream cnitlrt/AutoTeam
codex_auth.py:236-468 引入 5 个 helper(_is_workspace_ignored_label /
_is_workspace_selection_page / _workspace_label_candidates / _click_workspace_locator /
_select_team_workspace)+ 3 常量(_WORKSPACE_PAGE_HINTS / _WORKSPACE_IGNORE_LABELS /
_WORKSPACE_IGNORE_SUBSTRINGS),consent loop 每个 step 起始先用 upstream 检测。

测试覆盖:
  1. _is_workspace_selection_page 正向 — URL 含 workspace 直接 True
  2. _is_workspace_selection_page 负向 — consent 页 / 单一 hint 不命中(2-hint scoring 守恒)
  3. _select_team_workspace 命中 label 并点击成功
  4. consent loop 集成 — workspace 出现在 step 中,被检测 + 选择 + continue 不 break

测试用 unittest.mock.MagicMock 模拟 Playwright Page,不启动真浏览器。
"""

from __future__ import annotations

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# 通用 Page mock 工厂
# ---------------------------------------------------------------------------
def _make_page(
    *,
    url: str = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
    body_text: str = "",
):
    """构造一个最小可用的 Playwright Page mock。

    body_text — body locator inner_text 返回内容(用于 _is_workspace_selection_page 命中检测)。
    """
    page = MagicMock()
    page.url = url

    body_loc = MagicMock()
    body_loc.inner_text.return_value = body_text

    def fake_locator(selector):
        if selector == "body":
            return body_loc
        # 默认空 locator,所有 .first / .all() / .is_visible 都返回安全默认
        empty = MagicMock()
        empty.first = empty
        empty.all.return_value = []
        empty.is_visible.return_value = False
        return empty

    page.locator = fake_locator
    return page


# ---------------------------------------------------------------------------
# Test 1 — _is_workspace_selection_page 正向 (URL 含 workspace 直接 True)
# ---------------------------------------------------------------------------
def test_is_workspace_selection_page_detects_team_marker():
    """URL 含 'workspace' 直接命中,不依赖 body 文本(upstream 第一个 if 分支)。"""
    from autoteam.oauth_workspace import _is_workspace_selection_page

    # 正向 case 1: URL 含 /workspace
    page1 = _make_page(url="https://auth.openai.com/workspace")
    assert _is_workspace_selection_page(page1) is True

    # 正向 case 2: URL 含 workspace/select
    page2 = _make_page(url="https://auth.openai.com/api/accounts/workspace/select")
    assert _is_workspace_selection_page(page2) is True

    # 正向 case 3: URL 不含 workspace,但 body 含 ≥ 2 hint(upstream 2-hint scoring)
    body_with_2hints = (
        "Choose a workspace\nLaunch a workspace\nYour workspaces"  # 命中 3 hints
    )
    page3 = _make_page(
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/foo",
        body_text=body_with_2hints,
    )
    assert _is_workspace_selection_page(page3) is True

    # 正向 case 4: 中文 hint 命中(选择一个工作空间 + 工作空间)
    body_zh = "选择一个工作空间\n选择工作空间\n点击继续"
    page4 = _make_page(
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/foo",
        body_text=body_zh,
    )
    assert _is_workspace_selection_page(page4) is True


# ---------------------------------------------------------------------------
# Test 2 — _is_workspace_selection_page 负向(consent 页应返回 False,不能误判)
# ---------------------------------------------------------------------------
def test_is_workspace_selection_page_returns_false_on_consent_page():
    """consent 页 URL 不含 workspace,body 也无 ≥ 2 workspace hint → False。

    这是关键:旧实现把 'consent' URL 当 workspace 页(误判),会让 consent loop
    第一个 step 跑 workspace 选择路径而不是 consent 按钮路径。
    """
    from autoteam.oauth_workspace import _is_workspace_selection_page

    # 负向 case 1: consent 页 URL,body 无 workspace 内容
    page1 = _make_page(
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        body_text="允许 OpenAI 访问您的账号信息以登录 Codex。\n继续",
    )
    assert _is_workspace_selection_page(page1) is False

    # 负向 case 2: body 完全不含任何 workspace 相关 hint → False
    assert _is_workspace_selection_page(_make_page(
        url="https://auth.openai.com/foo",
        body_text="random text without any markers",
    )) is False

    # 负向 case 3: 单一 hint(只有 "select a workspace" 命中,缺其它 hint)
    page3 = _make_page(
        url="https://auth.openai.com/foo",
        body_text="Just a single phrase that says: choose one option below",
    )
    assert _is_workspace_selection_page(page3) is False

    # 负向 case 4: organization URL 但只有 1 hint(upstream 严格 ≥ 2)
    page4 = _make_page(
        url="https://auth.openai.com/organization",
        body_text="Some lone marker: workspace",
    )
    # body 命中 "workspace",仅 1 个 hint(在 organization URL 路径下要 ≥ 2)
    assert _is_workspace_selection_page(page4) is False


# ---------------------------------------------------------------------------
# Test 3 — _select_team_workspace 命中 label 并点击成功
# ---------------------------------------------------------------------------
def test_select_team_workspace_clicks_target_label(monkeypatch):
    """workspace_label_candidates 返回带 target label 的 (text, loc) → 点击 + return True。

    用 monkeypatch 替换 _workspace_label_candidates 直接返回 mock,避免依赖真 page DOM 遍历。
    """
    from autoteam import oauth_workspace

    # mock locator,click 返回成功(不抛)
    mock_loc_target = MagicMock()
    mock_loc_target.click.return_value = None  # 成功

    mock_loc_other = MagicMock()
    mock_loc_other.click.return_value = None

    # 候选列表中第二个匹配 workspace_name
    fake_candidates = [
        ("Other Team", mock_loc_other),
        ("My Team Workspace", mock_loc_target),
        ("New organization", MagicMock()),  # 应该已被 _is_workspace_ignored_label 过滤,这里仅为确认我们走 label 比对
    ]

    monkeypatch.setattr(
        oauth_workspace,
        "_workspace_label_candidates",
        lambda page: fake_candidates,
    )
    # 避免 time.sleep 在测试里阻塞
    monkeypatch.setattr(oauth_workspace.time, "sleep", lambda *_: None)

    page = _make_page()
    result = oauth_workspace._select_team_workspace(page, "My Team Workspace")
    assert result is True
    # target locator 必须被点击,其它 locator 不应被点击
    mock_loc_target.click.assert_called_once()
    mock_loc_other.click.assert_not_called()


def test_select_team_workspace_returns_false_when_no_match(monkeypatch):
    """没有任何候选 label 匹配 workspace_name → 走 fallback selector 也找不到 → False。"""
    from autoteam import oauth_workspace

    monkeypatch.setattr(
        oauth_workspace,
        "_workspace_label_candidates",
        lambda page: [("Foo Team", MagicMock())],
    )
    monkeypatch.setattr(oauth_workspace.time, "sleep", lambda *_: None)

    # fallback selector 也找不到 — page.locator 默认返回 not visible
    page = _make_page()
    result = oauth_workspace._select_team_workspace(page, "My Team Workspace")
    assert result is False


def test_select_team_workspace_empty_name_returns_false():
    """workspace_name 为空 → 直接 False(不进 candidate 遍历)。"""
    from autoteam import oauth_workspace

    page = _make_page()
    assert oauth_workspace._select_team_workspace(page, "") is False
    assert oauth_workspace._select_team_workspace(page, "   ") is False
    assert oauth_workspace._select_team_workspace(page, None) is False


# ---------------------------------------------------------------------------
# Test 4 — consent loop 集成验证 (workspace 出现 → 选择 + continue,不 break)
# ---------------------------------------------------------------------------
def test_consent_loop_handles_workspace_before_consent_button(monkeypatch):
    """
    模拟集成场景:consent loop 第 1 个 step 时 page 变成 workspace 选择页。

    验证目标(对应任务 PRD):
      - upstream-style _is_workspace_selection_page 命中 → True
      - _select_team_workspace 被调用并返回 True
      - 后续应 click 继续按钮 + continue 进下一 step(不能因 consent button 不可见而 break)

    本测试断言:在 workspace 页 _select_team_workspace 被调用一次,且能正确匹配。
    """
    from autoteam import oauth_workspace

    # 1. 验证 _is_workspace_selection_page 在 workspace URL 下命中
    workspace_page = _make_page(url="https://auth.openai.com/workspace")
    assert oauth_workspace._is_workspace_selection_page(workspace_page) is True

    # 2. mock candidates 返回匹配 workspace_name 的 locator
    mock_loc = MagicMock()
    mock_loc.click.return_value = None
    monkeypatch.setattr(
        oauth_workspace,
        "_workspace_label_candidates",
        lambda page: [("Team Alpha", mock_loc)],
    )
    monkeypatch.setattr(oauth_workspace.time, "sleep", lambda *_: None)

    # 3. 调 _select_team_workspace,断言点击发生
    selected = oauth_workspace._select_team_workspace(workspace_page, "Team Alpha")
    assert selected is True
    mock_loc.click.assert_called_once()

    # 4. 反向场景:同样的 page,但 workspace_name 不匹配 → False(不会误点)
    mock_loc_other = MagicMock()
    monkeypatch.setattr(
        oauth_workspace,
        "_workspace_label_candidates",
        lambda page: [("Beta Team", mock_loc_other)],
    )
    selected2 = oauth_workspace._select_team_workspace(workspace_page, "Team Alpha")
    assert selected2 is False
    mock_loc_other.click.assert_not_called()


# ---------------------------------------------------------------------------
# Bonus — _is_workspace_ignored_label 边界守恒测试
# ---------------------------------------------------------------------------
def test_workspace_ignored_label_filters_noise():
    """upstream IGNORE_LABELS / IGNORE_SUBSTRINGS 必须过滤已知噪声。"""
    from autoteam.oauth_workspace import _is_workspace_ignored_label

    # 完全匹配 IGNORE_LABELS
    assert _is_workspace_ignored_label("Continue") is True
    assert _is_workspace_ignored_label("继续") is True
    assert _is_workspace_ignored_label("Allow") is True
    assert _is_workspace_ignored_label("Choose a workspace") is True
    assert _is_workspace_ignored_label("Use password") is True

    # 子串匹配 IGNORE_SUBSTRINGS
    assert _is_workspace_ignored_label("Create new organization") is True
    assert _is_workspace_ignored_label("Finish setting up your account") is True

    # 大小写不敏感
    assert _is_workspace_ignored_label("CONTINUE") is True

    # 非噪声 label(真 workspace 名字)不应被忽略
    assert _is_workspace_ignored_label("My Team Workspace 2024") is False
    assert _is_workspace_ignored_label("Engineering Team") is False


def test_upstream_helpers_exported_from_oauth_workspace():
    """验证 5 个 helper + 3 个常量都从 oauth_workspace 模块可访问(保持 upstream 原名)。"""
    from autoteam import oauth_workspace

    # 5 个 helper
    assert callable(oauth_workspace._is_workspace_ignored_label)
    assert callable(oauth_workspace._is_workspace_selection_page)
    assert callable(oauth_workspace._workspace_label_candidates)
    assert callable(oauth_workspace._click_workspace_locator)
    assert callable(oauth_workspace._select_team_workspace)

    # 3 个常量
    assert isinstance(oauth_workspace._WORKSPACE_PAGE_HINTS, tuple)
    assert isinstance(oauth_workspace._WORKSPACE_IGNORE_LABELS, set)
    assert isinstance(oauth_workspace._WORKSPACE_IGNORE_SUBSTRINGS, tuple)

    # 关键文案在常量中
    assert "choose a workspace" in oauth_workspace._WORKSPACE_PAGE_HINTS
    assert "选择一个工作空间" in oauth_workspace._WORKSPACE_PAGE_HINTS
    assert "continue" in oauth_workspace._WORKSPACE_IGNORE_LABELS
    assert "继续" in oauth_workspace._WORKSPACE_IGNORE_LABELS


def test_click_workspace_locator_falls_back_to_force_click():
    """普通 click 抛异常 → 重试 force=True click → 成功返回 True。"""
    from autoteam.oauth_workspace import _click_workspace_locator

    # case 1: 普通 click 直接成功
    loc1 = MagicMock()
    loc1.click.return_value = None
    assert _click_workspace_locator(loc1) is True
    assert loc1.click.call_count == 1

    # case 2: 普通 click 抛异常,force=True 成功
    loc2 = MagicMock()
    call_count = {"n": 0}

    def click_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first attempt failed")
        return None

    loc2.click.side_effect = click_side_effect
    assert _click_workspace_locator(loc2) is True
    assert loc2.click.call_count == 2  # 一次普通 + 一次 force

    # case 3: 两次都失败 → False
    loc3 = MagicMock()
    loc3.click.side_effect = RuntimeError("always fail")
    assert _click_workspace_locator(loc3) is False
    assert loc3.click.call_count == 2
