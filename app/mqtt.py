"""MQTT-based device discovery for Tasmota.

Tasmota devices subscribed to a group command topic (default `tasmotas`) reply to a
`Status 0` request with their full status over MQTT. We collect those replies for a
short window and extract IP / MAC / name / version. The actual backup still happens
over HTTP afterwards (via service.add_device), so HTTP reachability is still required.

Mirrors the original PHP discovery (lib/mqtt.inc.php) but async via aiomqtt.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiomqtt

log = logging.getLogger("tasmobackup.mqtt")


@dataclass
class Discovered:
    ip: str
    mac: str = ""
    name: str = ""
    version: str = ""
    topic: str = ""


# Wildcard response topics covering both common FullTopic orderings:
#   %prefix%/%topic%/  -> stat/<topic>/STATUSx
#   %topic%/%prefix%/  -> <topic>/stat/STATUSx
_SUB_TOPICS = (
    "stat/+/STATUS", "stat/+/STATUS2", "stat/+/STATUS5",
    "+/stat/STATUS", "+/stat/STATUS2", "+/stat/STATUS5",
)


def _json(payload: str | None) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return {}


async def discover(
    host: str,
    port: int = 1883,
    user: str = "",
    password: str = "",
    group_topic: str = "tasmotas",
    window: float = 8.0,
) -> list[Discovered] | None:
    """Return discovered devices, or None if the broker connection failed."""
    group_topic = group_topic or "tasmotas"
    found: dict[str, dict[str, str]] = {}
    try:
        async with aiomqtt.Client(
            hostname=host,
            port=int(port),
            username=user or None,
            password=password or None,
            identifier="TasmoBackup",
            timeout=5.0,
        ) as client:
            for topic in _SUB_TOPICS:
                await client.subscribe(topic)

            # Trigger replies. Publish to both FullTopic orderings, and to the legacy
            # 'sonoffs' group when using the default topic.
            groups = [group_topic] + (["sonoffs"] if group_topic == "tasmotas" else [])
            for grp in groups:
                await client.publish(f"cmnd/{grp}/STATUS", "0")
                await client.publish(f"{grp}/cmnd/STATUS", "0")

            try:
                async with asyncio.timeout(window):
                    async for msg in client.messages:
                        topic = str(msg.topic)
                        key, _, suffix = topic.rpartition("/")
                        payload = bytes(msg.payload).decode(errors="ignore")
                        slot = found.setdefault(key, {})
                        if suffix == "STATUS":
                            slot["status"] = payload
                        elif suffix == "STATUS2":
                            slot["status2"] = payload
                        elif suffix == "STATUS5":
                            slot["status5"] = payload
            except asyncio.TimeoutError:
                pass
    except (aiomqtt.MqttError, OSError) as exc:
        log.warning("MQTT discovery failed: %s", exc)
        return None

    results: list[Discovered] = []
    for key, slot in found.items():
        net = _json(slot.get("status5")).get("StatusNET", {})
        ip = net.get("IPAddress") or net.get("IP")  # IPAddress >= 5.12.0, IP before
        if not ip:
            continue
        status = _json(slot.get("status")).get("Status", {})
        name = (
            status.get("DeviceName")
            or (status.get("FriendlyName") or [""])[0]
            or status.get("Topic", "")
        )
        version = _json(slot.get("status2")).get("StatusFWR", {}).get("Version", "")
        results.append(
            Discovered(
                ip=str(ip),
                mac=str(net.get("Mac", "")).upper(),
                name=str(name).strip(),
                version=str(version),
                topic=key,
            )
        )
    return results
