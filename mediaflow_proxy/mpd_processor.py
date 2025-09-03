import asyncio
import logging
import math
import time
from io import BytesIO

from fastapi import Request, Response, HTTPException

from mediaflow_proxy.drm.decrypter import decrypt_segment
from mediaflow_proxy.utils.crypto_utils import encryption_handler
from mediaflow_proxy.utils.http_utils import encode_mediaflow_proxy_url, get_original_scheme, ProxyRequestHeaders
from mediaflow_proxy.utils.dash_prebuffer import dash_prebuffer
from mediaflow_proxy.configs import settings

logger = logging.getLogger(__name__)


async def process_manifest(
    request: Request, mpd_dict: dict, proxy_headers: ProxyRequestHeaders, key_id: str = None, key: str = None
) -> Response:
    """
    Processes the MPD manifest and converts it to an HLS manifest.
    """
    hls_content = build_hls(mpd_dict, request, key_id, key)
    
    # Start DASH pre-buffering in background if enabled
    if settings.enable_dash_prebuffer:
        headers = {k[2:]: v for k, v in request.query_params.items() if k.startswith("h_")}
        mpd_url = request.query_params.get("d", "")
        if mpd_url:
            task = asyncio.create_task(
                dash_prebuffer.prebuffer_dash_manifest(mpd_url, headers)
            )
            task.add_done_callback(
                lambda t: logger.error("Prebuffer failed", exc_info=t.exception())
                if t.exception() else None
            )
    
    return Response(content=hls_content, media_type="application/vnd.apple.mpegurl", headers=proxy_headers.response)


async def process_playlist(
    request: Request, mpd_dict: dict, profile_id: str, proxy_headers: ProxyRequestHeaders
) -> Response:
    """
    Processes the MPD manifest and converts it to an HLS playlist for a specific profile.
    """
    matching_profiles = [p for p in mpd_dict["profiles"] if p["id"] == profile_id]
    if not matching_profiles:
        raise HTTPException(status_code=404, detail="Profile not found")

    hls_content = build_hls_playlist(mpd_dict, matching_profiles, request)
    return Response(content=hls_content, media_type="application/vnd.apple.mpegurl", headers=proxy_headers.response)


async def process_segment(
    init_content: bytes,
    segment_content: bytes,
    mimetype: str,
    proxy_headers: ProxyRequestHeaders,
    key_id: str = None,
    key: str = None,
) -> Response:
    """
    Processes and decrypts a media segment.
    """
    if key_id and key:
        # For DRM protected content
        now = time.time()
        decrypted_content = decrypt_segment(init_content, segment_content, key_id, key)
        logger.info(f"Decryption of {mimetype} segment took {time.time() - now:.4f} seconds")
    else:
        # For non-DRM protected content, concatenate efficiently
        buf = BytesIO()
        buf.write(init_content)
        buf.write(segment_content)
        decrypted_content = buf.getvalue()

    return Response(content=decrypted_content, media_type=mimetype, headers=proxy_headers.response)


def build_hls(mpd_dict: dict, request: Request, key_id: str = None, key: str = None) -> str:
    """
    Builds an HLS manifest from the MPD manifest.
    """
    hls = ["#EXTM3U", "#EXT-X-VERSION:6"]
    query_params = dict(request.query_params)
    has_encrypted = query_params.pop("has_encrypted", "false").lower() == "true"

    video_profiles = {}
    audio_profiles = {}

    proxy_url = request.url_for("playlist_endpoint")
    proxy_url = str(proxy_url.replace(scheme=get_original_scheme(request)))

    for profile in mpd_dict["profiles"]:
        # ⚠️ TODO: evitare key_id e key in query string (usare header o token sicuro)
        query_params.update({"profile_id": profile["id"], "key_id": key_id or "", "key": key or ""})
        playlist_url = encode_mediaflow_proxy_url(
            proxy_url,
            query_params=query_params,
            encryption_handler=encryption_handler if has_encrypted else None,
        )

        if "video" in profile["mimeType"]:
            video_profiles[profile["id"]] = (profile, playlist_url)
        elif "audio" in profile["mimeType"]:
            audio_profiles[profile["id"]] = (profile, playlist_url)

    # Add audio streams
    for i, (profile, playlist_url) in enumerate(audio_profiles.values()):
        is_default = "YES" if i == 0 else "NO"
        hls.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{profile["id"]}",DEFAULT={is_default},AUTOSELECT={is_default},LANGUAGE="{profile.get("lang", "und")}",URI="{playlist_url}"'
        )

    # Add video streams
    for profile, playlist_url in video_profiles.values():
        audio_attr = ',AUDIO="audio"' if audio_profiles else ""
        hls.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={profile["bandwidth"]},RESOLUTION={profile["width"]}x{profile["height"]},CODECS="{profile["codecs"]}",FRAME-RATE={profile["frameRate"]}{audio_attr}'
        )
        hls.append(playlist_url)

    return "\n".join(hls)


def build_hls_playlist(mpd_dict: dict, profiles: list[dict], request: Request) -> str:
    """
    Builds an HLS playlist from the MPD manifest for specific profiles.
    """
    hls = ["#EXTM3U", "#EXT-X-VERSION:6"]

    added_segments = 0

    proxy_url = request.url_for("segment_endpoint")
    proxy_url = str(proxy_url.replace(scheme=get_original_scheme(request)))

    for index, profile in enumerate(profiles):
        segments = profile["segments"]
        if not segments:
            logger.warning(f"No segments found for profile {profile['id']}")
            continue

        if index == 0:
            first_segment = segments[0]
            extinf_values = [f["extinf"] for f in segments if "extinf" in f]
            target_duration = math.ceil(max(extinf_values)) if extinf_values else 3

            mpd_start_number = profile.get("segment_template_start_number")
            if mpd_start_number and mpd_start_number >= 1000:
                sequence = first_segment.get("number", mpd_start_number)
            else:
                time_val = first_segment.get("time")
                duration_val = first_segment.get("duration_mpd_timescale")
                if time_val is not None and duration_val and duration_val > 0:
                    calculated_sequence = math.floor(time_val / duration_val)
                    if mpd_dict.get("isLive", False) and calculated_sequence > 100000:
                        sequence = calculated_sequence % 100000
                    else:
                        sequence = calculated_sequence
                else:
                    sequence = first_segment.get("number", 1)

            hls.extend(
                [
                    f"#EXT-X-TARGETDURATION:{target_duration}",
                    f"#EXT-X-MEDIA-SEQUENCE:{sequence}",
                ]
            )
            if mpd_dict["isLive"]:
                # EVENT vs LIVE → rendilo configurabile
                hls.append("#EXT-X-PLAYLIST-TYPE:LIVE")
            else:
                hls.append("#EXT-X-PLAYLIST-TYPE:VOD")

        init_url = profile["initUrl"]

        query_params = dict(request.query_params)
        query_params.pop("profile_id", None)
        query_params.pop("d", None)
        has_encrypted = query_params.pop("has_encrypted", "false").lower() == "true"

        for segment in segments:
            hls.append(f'#EXTINF:{segment["extinf"]:.3f},')
            query_params.update(
                {"init_url": init_url, "segment_url": segment["media"], "mime_type": profile["mimeType"]}
            )
            hls.append(
                encode_mediaflow_proxy_url(
                    proxy_url,
                    query_params=query_params,
                    encryption_handler=encryption_handler if has_encrypted else None,
                )
            )
            added_segments += 1

    if not mpd_dict["isLive"]:
        hls.append("#EXT-X-ENDLIST")

    logger.info(f"Added {added_segments} segments to HLS playlist")
    return "\n".join(hls)
