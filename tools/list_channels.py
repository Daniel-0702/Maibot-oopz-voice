"""列出 OOPZ 账号加入的域和频道，打印可以直接粘贴的 ID。

OOPZ 客户端界面只显示频道名，不显示 ID；这个脚本通过官方 SDK 的 REST 接口
把「域 → 分组 → 频道」整棵树打出来，并标出哪些是语音频道。

用法（在语音桥目录下）：

    .\\.venv\\Scripts\\python tools\\list_channels.py
    .\\.venv\\Scripts\\python tools\\list_channels.py --area <域ID或域名>
    .\\.venv\\Scripts\\python tools\\list_channels.py --json

凭证和 bridge.py 一致：四件套（OOPZ_DEVICE_ID 等）或手机号密码
（OOPZ_LOGIN_PHONE / OOPZ_LOGIN_PASSWORD）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _clean(value: str) -> str:
    """控制台复制长 JWT 时可能混进换行，清掉。"""
    return "".join(str(value or "").split())


async def build_config(args: argparse.Namespace):
    from oopz_sdk import OopzConfig

    device_id = _clean(args.device_id or os.environ.get("OOPZ_DEVICE_ID", ""))
    person_uid = _clean(args.person_uid or os.environ.get("OOPZ_PERSON_UID", ""))
    jwt_token = _clean(args.jwt_token or os.environ.get("OOPZ_JWT_TOKEN", ""))
    private_key = (args.private_key or os.environ.get("OOPZ_PRIVATE_KEY", "")).replace("\\n", "\n")

    if device_id and person_uid and jwt_token:
        return OopzConfig(
            device_id=device_id,
            person_uid=person_uid,
            jwt_token=jwt_token,
            private_key=private_key,
        )

    # 手机号 + 密码：写进环境变量，复用与 bridge.py 相同的登录流程
    if args.phone:
        os.environ["OOPZ_LOGIN_PHONE"] = args.phone
    if args.password:
        os.environ["OOPZ_LOGIN_PASSWORD"] = args.password
    from_env_async = getattr(OopzConfig, "from_env_async", None)
    if from_env_async is not None:
        return await from_env_async()
    return OopzConfig.from_env()


async def run(args: argparse.Namespace) -> int:
    from oopz_sdk import OopzRESTClient

    config = await build_config(args)
    rows: list[dict] = []

    async with OopzRESTClient(config) as client:
        areas = await client.areas.get_joined_areas()
        if not areas:
            print("账号没有加入任何域。先把小号拉进目标域。", file=sys.stderr)
            return 1

        for area in areas:
            area_id = str(getattr(area, "area_id", "") or "")
            area_name = str(getattr(area, "name", "") or "")
            if args.area and args.area not in {area_id, area_name}:
                continue
            if not args.json:
                print("=" * 60)
                print(f"[域] {area_name}")
                print(f"     域 ID (AreaId): {area_id}")
                print("=" * 60)

            groups = await client.areas.get_area_channels(area_id)
            for group in groups:
                group_name = str(getattr(group, "name", "") or "")
                if not args.json:
                    print(f"  [分组] {group_name}")
                for ch in getattr(group, "channels", []) or []:
                    channel_id = str(getattr(ch, "channel_id", "") or "")
                    channel_name = str(getattr(ch, "name", "") or "")
                    channel_type = str(getattr(ch, "channel_type", "") or "").upper()
                    rows.append(
                        {
                            "area_id": area_id,
                            "area_name": area_name,
                            "group_id": str(getattr(group, "group_id", "") or ""),
                            "group_name": group_name,
                            "channel_id": channel_id,
                            "name": channel_name,
                            "type": channel_type,
                        }
                    )
                    if not args.json:
                        kind = "语音" if channel_type in {"VOICE", "AUDIO"} else "文字"
                        print(f"    [{kind}] {channel_name}   id={channel_id}")

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    voice = [r for r in rows if r["type"] in {"VOICE", "AUDIO"}]
    print()
    if not voice:
        print("这个域里没有找到语音频道（VOICE/AUDIO）。", file=sys.stderr)
        return 1

    print("=" * 60)
    print("把下面几行填进 start_bridge.ps1 顶部：")
    first = voice[0]
    print(f'$AreaId    = "{first["area_id"]}"     # 域 ID')
    print(f'$ChannelId = "{first["channel_id"]}"     # 频道 ID')
    print()
    print("注意：$ChannelId 也可以直接写频道名，例如：")
    print(f'$ChannelId = "{first["name"]}"')
    if len(voice) > 1:
        print()
        print(f"这个域（{first['area_id']}）里还有其他语音频道：")
        for item in voice[1:]:
            print(f'  "{item["name"]}"  ->  {item["channel_id"]}')
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="列出 OOPZ 域和频道")
    parser.add_argument("--area", default="", help="只列某一个域（ID 或名字）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--phone", default="", help="OOPZ 手机号（不填则读环境变量）")
    parser.add_argument("--password", default="", help="OOPZ 密码（不填则读环境变量）")
    parser.add_argument("--device-id", default="", help="四件套：device_id")
    parser.add_argument("--person-uid", default="", help="四件套：person_uid")
    parser.add_argument("--jwt-token", default="", help="四件套：jwt_token")
    parser.add_argument("--private-key", default="", help="四件套：RSA 私钥，换行写 \\n")
    return parser.parse_args()


def main() -> None:
    try:
        sys.exit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
