from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Callable

from fph.config import ProviderConfig
from fph.providers.base import AuthRequiredError, ProviderError

MARKER_START = "__FPH_JSON_START__"
MARKER_END = "__FPH_JSON_END__"

CommandRunner = Callable[[list[str], str, int], tuple[int, str, str]]

TASK_SPACE_HELPER_JS = """
async function getOrCreatePage(spaceName) {
  const existing = (await listTaskSpaces()).find((s) => s.name === spaceName);
  const task = !existing
    ? await taskSpace(spaceName)
    : existing.ownership === "user"
      ? await claimTaskSpace(existing.id)
      : existing.ownership === "agentDelegatedToUser"
        ? await takeOverTaskSpace(existing.id)
        : await taskSpace(existing.id);
  const pages = await task.pages();
  let page = pages[0];
  if (!page) {
    const tabs = await task.tabs();
    page = tabs.length > 0 ? await task.adopt(tabs[0].page) : await task.newPage();
  }
  return { task, page };
}
""".strip()

_USER_CONTROL_HINTS = (
    "The user now controls this task space",
    "permission prompt",
    "Wait for the user to handle",
)


def _default_command_runner(cmd: list[str], stdin_text: str, timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        input=stdin_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _extract_marked_json(output: str) -> dict[str, Any]:
    start_idx = output.find(MARKER_START)
    end_idx = output.find(MARKER_END, start_idx + len(MARKER_START)) if start_idx != -1 else -1
    if start_idx == -1 or end_idx == -1:
        preview = output.strip()[-400:] if output.strip() else "(empty output)"
        raise ProviderError(f"未能从 ego-browser 输出中解析结果标记。输出尾部: {preview}")
    payload_str = output[start_idx + len(MARKER_START) : end_idx].strip()
    try:
        data = json.loads(payload_str)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"ego-browser 返回了无效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("ego-browser 返回的 JSON 根对象不是 dict。")
    return data


def _to_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except ValueError:
        return None


class EgoBrowserRunner:
    """Shared runtime wrapper for driving local ego(lite) / ego-browser nodejs."""

    def __init__(
        self,
        config: ProviderConfig,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or _default_command_runner

    def check_ready(self) -> tuple[bool, str]:
        resolved = shutil.which(self.config.ego_browser_cmd)
        if not resolved:
            return (
                False,
                f"未找到 `{self.config.ego_browser_cmd}` 命令。请先安装 ego(lite) 桌面端并确保 ego-browser 已加入 PATH。",
            )
        return True, f"已检测到 ego-browser: {resolved}（Task Space: {self.config.task_space}）"

    def run_script(self, script: str) -> dict[str, Any]:
        cmd = [self.config.ego_browser_cmd, "nodejs"]
        try:
            code, stdout, stderr = self._runner(cmd, script, self.config.timeout_seconds)
        except FileNotFoundError as exc:
            raise ProviderError(
                f"未找到命令 `{self.config.ego_browser_cmd}`。请先安装 ego(lite) 或在 config.toml 中配置完整路径。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"ego-browser 执行超时（>{self.config.timeout_seconds}s）。") from exc

        combined = f"{stdout}\n{stderr}" if stderr else stdout
        if MARKER_START in combined:
            payload = _extract_marked_json(combined)
            if payload.get("error"):
                raise ProviderError(f"ego-browser 脚本报错: {payload['error']}")
            if payload.get("authRequired"):
                raise AuthRequiredError(
                    str(payload.get("reason") or "需要登录或人机验证"),
                    url=str(payload.get("currentUrl") or ""),
                    task_space=self.config.task_space,
                )
            return payload

        if any(hint in combined for hint in _USER_CONTROL_HINTS):
            raise AuthRequiredError(
                "检测到浏览器权限弹窗或人工接管提示，请在 ego(lite) 对应 Space 中确认弹窗或完成后重跑",
                task_space=self.config.task_space,
            )

        if code != 0:
            err_msg = (stderr or stdout).strip()[-500:]
            raise ProviderError(f"ego-browser 退出码 {code}: {err_msg}")

        return _extract_marked_json(combined)

    def check_auth_cleared(self, platform: str) -> bool:
        """Read-only probe checking whether the user already cleared auth in the visible page."""
        cfg_json = json.dumps(
            {
                "taskSpace": self.config.task_space,
                "platform": platform,
                "markerStart": MARKER_START,
                "markerEnd": MARKER_END,
            },
            ensure_ascii=False,
        )
        script = f"""
const CFG = {cfg_json};
{TASK_SPACE_HELPER_JS}

async function run() {{
  const {{ page }} = await getOrCreatePage(CFG.taskSpace);
  const state = await page.evaluate((plat) => {{
    const href = location.href || "";
    const txt = (document.body && document.body.innerText) ? document.body.innerText : "";
    if (plat === "taobao") {{
      const badUrl = /login\\.taobao\\.com|sec\\.taobao\\.com|bixi|punish|verify/i.test(href);
      const badBody = /亲，请拖动下方滑块|安全验证|访问被拒绝|人机验证|扫码登录|密码登录/.test(txt) && txt.length < 1500;
      const cleared = !badUrl && !badBody && /taobao\\.com/i.test(href) && txt.length >= 200;
      return {{ cleared }};
    }}
    if (plat === "jd") {{
      const badUrl = /passport\\.jd\\.com|plogin\\.m\\.jd\\.com|safe\\.jd\\.com|cfe\\.m\\.jd\\.com|verify/i.test(href);
      const badBody = /扫码登录|账号登录|请登录|安全验证|滑块验证|人机验证/.test(txt) && txt.length < 1500;
      const cleared = !badUrl && !badBody && /jd\\.com/i.test(href) && txt.length >= 200;
      return {{ cleared }};
    }}
    return {{ cleared: false }};
  }}, CFG.platform);
  console.log(CFG.markerStart + JSON.stringify(state) + CFG.markerEnd);
}}

run().catch((err) => {{
  console.log(CFG.markerStart + JSON.stringify({{ cleared: false, probeError: String(err) }}) + CFG.markerEnd);
}});
""".strip()
        try:
            payload = self.run_script(script)
            return bool(payload.get("cleared"))
        except Exception:
            return False
