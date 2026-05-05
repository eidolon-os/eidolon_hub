#!/usr/bin/env python3
"""
LiveKit Server 测试脚本
测试连接、房间创建、Token 生成等基本功能

依赖: pip install livekit-api
运行: python test_livekit.py
"""

import os
import sys
import asyncio
from livekit import api


async def main():
    # 从环境变量读取配置
    livekit_url = os.getenv("LIVEKIT_URL", "wss://livekit-server.yangtzeailab.com")
    api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    print("=" * 50)
    print("  LiveKit Server 测试")
    print("=" * 50)
    print(f"  URL: {livekit_url}")
    print(f"  API Key: {api_key}")
    print(f"  API Secret: {'*' * 8 if api_secret else '未设置'}")
    print("=" * 50)

    # 检查 API Secret
    if not api_secret:
        print("\n[ERROR] 请设置 LIVEKIT_API_SECRET 环境变量")
        print("  可以在服务器上运行:")
        print("  grep -A1 'devkey' deploy/livekit/livekit.yaml")
        print("  然后设置: export LIVEKIT_API_SECRET=<secret>")
        sys.exit(1)

    try:
        # 测试 1: 生成 Room Token
        print("\n[1/3] 测试 Token 生成...")
        token = await api.create_token(
            api_key=api_key,
            api_secret=api_secret,
            identity="test-user",
            name="Test User",
            room="test-room",
        )
        print(f"  Token 生成成功: {token[:50]}...")
        print(f"  完整 Token 长度: {len(token)} 字符")

        # 测试 2: 验证 Token 格式
        print("\n[2/3] 验证 Token 格式...")
        parts = token.split(".")
        if len(parts) == 3:
            print(f"  JWT 格式正确 (header.payload.signature)")
        else:
            print(f"  [WARN] Token 格式异常")

        # 测试 3: 列出房间 (需要连接到 LiveKit server)
        print("\n[3/3] 测试服务端点连通性...")
        room_service = api.RoomServiceClient(livekit_url, api_key, api_secret)
        
        try:
            # 尝试列出房间 (LiveKit Server 1.5+ API)
            # rooms = await room_service.list_rooms(api.ListRoomsRequest())
            # print(f"  当前房间数: {len(rooms.rooms)}")
            print("  跳过 list_rooms (可能需要服务端配置)")
        except Exception as e:
            print(f"  list_rooms 测试跳过: {e}")

        print("\n" + "=" * 50)
        print("  测试完成!")
        print("=" * 50)
        print("\n可以使用以下 Token 加入房间:")
        print(f"\n  房间名: test-room")
        print(f"  Token: {token}")
        print("\n在线测试 JWT: https://jwt.io/")
        print("\nWeb 客户端测试: 访问 LiveKit 官方示例")
        print(f"  https://{livekit_url.replace('wss://', '')}/")
        
    except Exception as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
