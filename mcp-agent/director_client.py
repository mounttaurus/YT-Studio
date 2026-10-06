"""director-agent(:8005) への薄いHTTPクライアント。

tools.py はこのクライアント経由でのみ外部I/Oする（純粋な合成ロジックと
ネットワークI/Oを分離＝後継のワンパッケージ移植時に差し替えやすくする）。
"""
import httpx

from config import DIRECTOR_URL, HTTP_TIMEOUT_READ, HTTP_TIMEOUT_WRITE


class DirectorError(RuntimeError):
    """director もしくは中継先コンテナからのエラーを包む。"""


async def get(path: str, params: dict | None = None, timeout: float | None = None) -> dict | list:
    """director の読み取りエンドポイントを叩いてJSONを返す。timeout 省略は既定（読み取り用）。"""
    url = f"{DIRECTOR_URL}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout or HTTP_TIMEOUT_READ) as client:
        try:
            res = await client.get(url, params=params)
        except httpx.RequestError as e:
            raise DirectorError(f"director unreachable ({url}): {e}") from e
    if res.status_code >= 400:
        raise DirectorError(f"GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def request(method: str, path: str, params: dict | None = None,
                  json: dict | None = None, data: dict | None = None) -> dict | list:
    """更新系/中継系。director の汎用プロキシ(/api/...)もここを通す。

    json= はJSONボディ、data= は form-urlencoded ボディ（Form受けのエンドポイント用）。
    """
    url = f"{DIRECTOR_URL}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_WRITE) as client:
        try:
            res = await client.request(method, url, params=params, json=json, data=data)
        except httpx.RequestError as e:
            raise DirectorError(f"director unreachable ({url}): {e}") from e
    if res.status_code >= 400:
        raise DirectorError(f"{method} {path} -> {res.status_code}: {res.text[:300]}")
    ctype = res.headers.get("content-type", "")
    return res.json() if "application/json" in ctype else {"raw": res.text}


async def get_bytes(path: str, params: dict | None = None, timeout: float | None = None) -> bytes:
    """director 経由でバイナリ（画像など）を取る。"""
    url = f"{DIRECTOR_URL}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout or HTTP_TIMEOUT_READ) as client:
        try:
            res = await client.get(url, params=params)
        except httpx.RequestError as e:
            raise DirectorError(f"director unreachable ({url}): {e}") from e
    if res.status_code >= 400:
        raise DirectorError(f"GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.content
