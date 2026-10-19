
import os
import logging
import ipaddress
import socket
from urllib.parse import urlparse

import requests
from flask import (
    Flask,
    request,
    Response,
    jsonify,
    stream_with_context,
)
from requests.exceptions import RequestException

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("video_proxy")

TIKWM_API = "https://www.tikwm.com/api/"
TIMEOUT = (10, 30)
CHUNK_SIZE = 64 * 1024
MAX_URL_LENGTH = 2048

# TikWM の動画配信先として実際に確認できたドメインを設定する。
# 例: VIDEO_ALLOWED_HOSTS=t3video.com,example-cdn.com
# example-cdn.com は設定例なので、そのまま登録しないこと。
VIDEO_ALLOWED_HOSTS = {
    host.strip().lower().rstrip(".")
    for host in os.environ.get(
        "VIDEO_ALLOWED_HOSTS", "t3video.com"
    ).split(",")
    if host.strip()
}

TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}


def host_is_allowed(host, allowed_hosts):
    """完全一致、または許可ドメインのサブドメインか判定する。"""
    host = host.lower().rstrip(".")
    return any(
        host == domain or host.endswith("." + domain)
        for domain in allowed_hosts
    )


def valid_https_url(url):
    """HTTPS URL の基本的な形式を検証する。"""
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def valid_tiktok_url(url):
    """許可された TikTok ドメインの URL のみ受け付ける。"""
    if not valid_https_url(url):
        return False

    host = urlparse(url).hostname.lower().rstrip(".")
    return host in TIKTOK_HOSTS


def valid_video_url(url):
    """設定済みの動画配信先ドメインのみ受け付ける。"""
    if not valid_https_url(url):
        return False

    host = urlparse(url).hostname.lower().rstrip(".")

    # 外部ドメインの IP アドレス指定を拒否
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    return host_is_allowed(host, VIDEO_ALLOWED_HOSTS)


def error_response(stage, message, status=502):
    logger.warning("Proxy failed: stage=%s error=%s", stage, message)
    return jsonify({
        "error": message,
        "stage": stage,
    }), status


@app.get("/")
def index():
    return jsonify({
        "service": "Video Streaming Proxy",
        "status": "ok",
        "endpoint": "/proxy?url=<TikTok URL>",
    })


@app.get("/proxy")
def proxy():
    stage = "validate_input"
    api_response = None
    video_response = None

    source_url = request.args.get("url", "").strip()

    if not source_url:
        return error_response(
            stage, "url parameter is required", 400
        )

    if len(source_url) > MAX_URL_LENGTH:
        return error_response(
            stage, "URL is too long", 400
        )

    if not valid_tiktok_url(source_url):
        return error_response(
            stage, "Invalid or unsupported TikTok URL", 400
        )

    try:
        # 1. TikWM API に動画 URL を渡して解析
        stage = "tikwm_api"

        api_response = requests.get(
            TIKWM_API,
            params={"url": source_url, "hd": "1"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        api_response.raise_for_status()

        api_data = api_response.json()

        if not isinstance(api_data, dict):
            return error_response(
                stage, "TikWM returned an invalid JSON object"
            )

        if api_data.get("code") != 0:
            logger.warning(
                "TikWM API error response: %s",
                api_data.get("msg", "unknown error"),
            )
            return error_response(
                stage, "TikWM could not process this video URL"
            )

        data = api_data.get("data") or {}
        video_url = data.get("play")

        if not isinstance(video_url, str) or not video_url:
            return error_response(
                stage, "TikWM did not return data.play"
            )

        # 2. 動画配信先を検証
        stage = "validate_video_url"

        if not valid_video_url(video_url):
            logger.warning(
                "Video URL host is not allowed: %s",
                urlparse(video_url).hostname,
            )
            return error_response(
                stage,
                "Video host is not allowed. Configure "
                "VIDEO_ALLOWED_HOSTS with the verified host."
            )

        # 3. Range リクエストを上流に転送
        stage = "video_download"

        upstream_headers = {
            "User-Agent": "Mozilla/5.0",
        }

        range_header = request.headers.get("Range")
        if range_header:
            upstream_headers["Range"] = range_header

        video_response = requests.get(
            video_url,
            headers=upstream_headers,
            stream=True,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        video_response.raise_for_status()

        if video_response.status_code not in (200, 206):
            return error_response(
                stage, "Unexpected upstream HTTP status"
            )

        # Content-Type を確認し、MP4 以外は拒否
        upstream_type = video_response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        if upstream_type not in (
            "video/mp4",
            "application/octet-stream",
        ):
            return error_response(
                stage, "Upstream did not return an MP4-compatible file"
            )

        response_headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": video_response.headers.get(
                "Accept-Ranges", "bytes"
            ),
        }

        for header in ("Content-Length", "Content-Range"):
            value = video_response.headers.get(header)
            if value:
                response_headers[header] = value

        # 4. チャンク単位でクライアントに転送
        def generate():
            try:
                for chunk in video_response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):
                    if chunk:
                        yield chunk
            except RequestException:
                logger.exception(
                    "Streaming failed: stage=stream_body"
                )
                # 送信開始後は JSON エラーに変更できない
                raise
            finally:
                video_response.close()

        response = Response(
            stream_with_context(generate()),
            status=video_response.status_code,
            content_type="video/mp4",
            headers=response_headers,
        )

        response.call_on_close(video_response.close)

        # Response がストリームを所有する
        video_response = None
        return response

    except (RequestException, ValueError) as exc:
        logger.exception(
            "Proxy request failed: stage=%s", stage
        )
        return error_response(
            stage, "Unable to retrieve video"
        )

    except Exception:
        logger.exception(
            "Unexpected proxy error: stage=%s", stage
        )
        return error_response(
            stage, "Internal server error"
        )

    finally:
        if api_response is not None:
            api_response.close()

        if video_response is not None:
            video_response.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
