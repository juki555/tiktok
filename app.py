
import os
import logging
from urllib.parse import urlparse

import requests
from flask import Flask, request, Response, jsonify, stream_with_context
from requests.exceptions import RequestException

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TIKWM_API = "https://www.tikwm.com/api/"
ALLOWED_HOSTS = {
    "www.tiktok.com",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
CHUNK_SIZE = 64 * 1024
TIMEOUT = (10, 30)


def valid_video_url(url):
    """許可した HTTPS 動画 URL だけを受け付ける。"""
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in ALLOWED_HOSTS
            and parsed.username is None
            and parsed.password is None
            and not parsed.port
        )
    except ValueError:
        return False


@app.get("/")
def index():
    return jsonify({
        "service": "Video Streaming Proxy",
        "endpoint": "/proxy?url=<TikTok URL>",
        "status": "ok",
    })


@app.get("/proxy")
def proxy():
    source_url = request.args.get("url", "").strip()

    if not source_url:
        return jsonify({"error": "url parameter is required"}), 400

    if len(source_url) > 2048 or not valid_video_url(source_url):
        return jsonify({"error": "Invalid or unsupported video URL"}), 400

    api_response = None
    video_response = None

    try:
        # 1. TikWM API から動画情報を取得
        api_response = requests.get(
            TIKWM_API,
            params={"url": source_url, "hd": "1"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=TIMEOUT,
        )
        api_response.raise_for_status()
        api_data = api_response.json()

        if api_data.get("code") != 0:
            return jsonify({
                "error": "Video API could not process the URL"
            }), 502

        video_url = (api_data.get("data") or {}).get("play")

        if not isinstance(video_url, str) or not video_url:
            return jsonify({
                "error": "Video URL was not returned by the API"
            }), 502

        # 2. 動画ファイルの URL を検証
        parsed_video = urlparse(video_url)
        if (
            parsed_video.scheme != "https"
            or parsed_video.hostname is None
            or parsed_video.hostname.lower() != "www.t3video.com"
            and not parsed_video.hostname.lower().endswith(".t3video.com")
        ):
            # TikWM の配信先ドメインは変更される可能性があります。
            # 実際の data.play のホストを確認し、必要なら
            # この許可リストを明示的に更新してください。
            return jsonify({
                "error": "Unsupported video hosting domain"
            }), 502

        # 3. 動画をストリーミング取得
        video_response = requests.get(
            video_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                **(
                    {"Range": request.headers["Range"]}
                    if "Range" in request.headers else {}
                ),
            },
            stream=True,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        video_response.raise_for_status()

        # Content-Length や Range など、再生に必要なヘッダーを引き継ぐ
        headers = {}
        for name in (
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
        ):
            if name in video_response.headers:
                headers[name] = video_response.headers[name]

        headers["Cache-Control"] = "no-store"
        headers["X-Content-Type-Options"] = "nosniff"

        def generate():
            try:
                for chunk in video_response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):
                    if chunk:
                        yield chunk
            finally:
                video_response.close()

        response = Response(
            stream_with_context(generate()),
            status=206 if video_response.status_code == 206 else 200,
            content_type="video/mp4",
            headers=headers,
        )
        response.call_on_close(video_response.close)
        video_response = None  # Response 側がストリームを管理
        return response

    except (RequestException, ValueError) as exc:
        logger.warning("Proxy request failed: %s", exc)
        return jsonify({
            "error": "Unable to retrieve video"
        }), 502

    except Exception:
        logger.exception("Unexpected proxy error")
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if api_response is not None:
            api_response.close()
        if video_response is not None:
            video_response.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
