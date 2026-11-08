from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import qrcode

from astrbot.api import logger

from .config import PluginConfig


class KuGouLogin:
    """Manage the single active KuGou QR login flow."""

    _USER_AGENT = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
    _QR_KEY_URL = "https://login-user.kugou.com/v2/qrcode"
    _QR_CHECK_URL = "https://login-user.kugou.com/v2/get_userinfo_qrcode"
    _QR_PAGE = "https://h5.kugou.com/apps/loginQRCode/html/index.html?qrcode="
    _APP_ID = "1005"
    _SRC_APP_ID = "2919"
    _CLIENT_VERSION = "20489"
    _SIGN_SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

    def __init__(self, config: PluginConfig):
        self.config = config
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._start_lock = asyncio.Lock()

    @staticmethod
    def _cookie_values(cookie: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key:
                values[key] = value
        return values

    @staticmethod
    def _signature(params: dict[str, Any]) -> str:
        params_string = "".join(f"{key}={params[key]}" for key in sorted(params))
        return hashlib.md5(
            f"{KuGouLogin._SIGN_SALT}{params_string}{KuGouLogin._SIGN_SALT}".encode()
        ).hexdigest()

    async def _request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, Any],
        cookies: dict[str, str],
    ) -> dict[str, Any]:
        """Send a signed KuGou login request.

        Args:
            session: Active HTTP session.
            url: KuGou login endpoint.
            params: Endpoint-specific query parameters.
            cookies: Cookies to send with the request.

        Returns:
            Parsed KuGou response.

        Raises:
            RuntimeError: If the endpoint returns a non-success HTTP status or
                a non-object response.
        """
        now = str(int(time.time()))
        request_params: dict[str, Any] = {
            "dfid": cookies.get("dfid") or uuid.uuid4().hex.upper()[:24],
            "mid": cookies.get("KUGOU_API_MID")
            or cookies.get("mid")
            or str(int(hashlib.md5(uuid.uuid4().bytes).hexdigest(), 16)),
            "uuid": "-",
            "appid": self._APP_ID,
            "clientver": self._CLIENT_VERSION,
            "clienttime": now,
            **params,
        }
        request_params["signature"] = self._signature(request_params)
        async with session.get(
            url,
            params=request_params,
            cookies=cookies,
            headers={"User-Agent": self._USER_AGENT},
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"KuGou login API returned HTTP {response.status}")
            body = await response.json(content_type=None)
            if not isinstance(body, dict):
                raise RuntimeError("KuGou login API returned an invalid response")
            return body

    async def start(
        self,
        on_qr: Callable[[bytes], Awaitable[None]],
        on_message: Callable[[str], Awaitable[None]],
    ) -> None:
        """Create a KuGou QR code and start polling for confirmation.

        Args:
            on_qr: Callback receiving the QR image as PNG bytes.
            on_message: Callback receiving duplicate-session, completion, timeout,
                or failure messages. It remains active during background polling.
        """
        async with self._start_lock:
            if self._task and not self._task.done():
                await on_message("已有酷狗音乐登录会话正在等待扫码")
                return

            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            self._session = session
            try:
                configured = self._cookie_values(
                    self.config.players.kugou.cookies or ""
                )
                configured["dfid"] = (
                    configured.get("dfid") or uuid.uuid4().hex.upper()[:24]
                )
                configured_mid = configured.get("KUGOU_API_MID") or configured.get(
                    "mid"
                )
                if not configured_mid or not configured_mid.isdigit():
                    configured_mid = str(
                        int(hashlib.md5(uuid.uuid4().bytes).hexdigest(), 16)
                    )
                configured["KUGOU_API_MID"] = configured_mid
                body = await self._request(
                    session,
                    self._QR_KEY_URL,
                    {
                        "appid": "1001",
                        "type": 1,
                        "plat": 4,
                        "qrcode_txt": (
                            "https://h5.kugou.com/apps/loginQRCode/html/index.html?"
                            f"appid={self._APP_ID}&"
                        ),
                        "srcappid": self._SRC_APP_ID,
                    },
                    configured,
                )
                data = body.get("data") or {}
                key = str(
                    data.get("qrcode") or data.get("key") or body.get("qrcode") or ""
                )
                if not key:
                    raise RuntimeError("KuGou QR response did not contain a QR key")
                qr = qrcode.make(f"{self._QR_PAGE}{key}")
                output = io.BytesIO()
                qr.save(output)
                await on_qr(output.getvalue())
                self._task = asyncio.create_task(
                    self._poll_login(on_message, session, key, configured)
                )
            except asyncio.CancelledError:
                await session.close()
                self._session = None
                raise
            except Exception as exc:
                await session.close()
                self._session = None
                logger.warning(f"[music] Could not start KuGou QR login: {exc}")
                await on_message(f"无法开始酷狗音乐登录：{exc}")

    async def _poll_login(
        self,
        on_message: Callable[[str], Awaitable[None]],
        session: aiohttp.ClientSession,
        key: str,
        configured: dict[str, str],
    ) -> None:
        current_task = asyncio.current_task()
        expires_at = time.monotonic() + 180
        last_status: object = None
        try:
            while time.monotonic() < expires_at:
                body = await self._request(
                    session,
                    self._QR_CHECK_URL,
                    {
                        "plat": 4,
                        "appid": self._APP_ID,
                        "srcappid": self._SRC_APP_ID,
                        "qrcode": key,
                    },
                    configured,
                )
                data = body.get("data") or {}
                status = str(data.get("status")) if isinstance(data, dict) else ""
                if status != last_status:
                    status_messages = {
                        "0": "酷狗二维码已过期",
                        "1": "等待扫描酷狗二维码",
                        "2": "酷狗二维码已扫描，等待确认",
                        "4": "酷狗扫码登录成功，正在保存登录状态",
                    }
                    if status in status_messages:
                        logger.info(status_messages[status])
                    last_status = status
                if status == "0":
                    await on_message("酷狗二维码已过期，请重新登录")
                    return
                if status == "4":
                    token = str(data.get("token") or "")
                    userid = str(data.get("userid") or "")
                    if not token or not userid:
                        raise RuntimeError("酷狗登录成功但响应缺少 token 或 userid")
                    configured["token"] = token
                    configured["userid"] = userid
                    for cookie_name in ("vip_token", "vip_type"):
                        value = data.get(cookie_name)
                        if value:
                            configured[cookie_name] = str(value)
                    configured["dfid"] = (
                        configured.get("dfid") or uuid.uuid4().hex.upper()[:24]
                    )
                    configured_mid = configured.get("KUGOU_API_MID") or configured.get(
                        "mid"
                    )
                    if not configured_mid or not configured_mid.isdigit():
                        configured_mid = str(
                            int(hashlib.md5(uuid.uuid4().bytes).hexdigest(), 16)
                        )
                    configured = {
                        key_: value
                        for key_, value in {
                            "token": configured["token"],
                            "userid": configured["userid"],
                            "dfid": configured["dfid"],
                            "KUGOU_API_MID": configured_mid,
                            "vip_token": configured.get("vip_token", ""),
                            "vip_type": configured.get("vip_type", ""),
                        }.items()
                        if value
                        and not any(char in f"{key_}{value}" for char in ";\r\n")
                    }
                    self.config.players.kugou.cookies = "; ".join(
                        f"{key_}={value}" for key_, value in configured.items()
                    )
                    self.config.save_config()
                    await on_message(f"酷狗登录成功，Cookie已写入配置。ID：{userid}")
                    return
                await asyncio.sleep(2)
            await on_message("酷狗登录二维码已超时，请重新登录")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[music] KuGou QR login failed: {exc}")
            with contextlib.suppress(Exception):
                await on_message(f"酷狗音乐登录失败：{exc}")
        finally:
            await session.close()
            if self._task is current_task:
                self._task = None
                self._session = None

    async def close(self) -> None:
        """Cancel the active login task and release its HTTP session."""
        async with self._start_lock:
            task = self._task
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if self._session and not self._session.closed:
                await self._session.close()
            self._task = None
            self._session = None
