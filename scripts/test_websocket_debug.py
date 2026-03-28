#!/usr/bin/env python3
"""Debug WebSocket connection to Atmeex - try different auth methods."""
import asyncio
import json
from datetime import datetime
from aiohttp import ClientSession, WSMsgType


API_BASE_URL = "https://api.iot.atmeex.com"
WS_ENDPOINT = "wss://ws.iot.atmeex.com"


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def login(email: str, password: str) -> str:
    """Login and get token."""
    print(f"🔐 [{ts()}] Авторизация...")
    async with ClientSession() as session:
        async with session.post(
            f"{API_BASE_URL}/auth/signin",
            json={"grant_type": "basic", "email": email, "password": password},
        ) as resp:
            data = await resp.json()
            token = data.get("access_token") or data.get("token")
            print(f"✅ [{ts()}] Token получен: {token[:30]}...")
            return token


async def test_method_1(token: str):
    """Method 1: Token in URL query parameter."""
    print(f"\n{'='*70}")
    print(f"🧪 МЕТОД 1: Token в URL (?token=...)")
    print(f"{'='*70}")
    
    async with ClientSession() as session:
        ws_url = f"{WS_ENDPOINT}?token={token}"
        print(f"URL: {ws_url[:60]}...")
        
        try:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                print(f"✅ [{ts()}] Подключено!")
                
                # Listen for 5 seconds
                try:
                    async with asyncio.timeout(5):
                        async for msg in ws:
                            print(f"📨 [{ts()}] Тип: {msg.type}, Данные: {msg.data}")
                            if msg.type == WSMsgType.CLOSE:
                                print(f"❌ Закрыто: код={msg.data}, extra={msg.extra}")
                                return False
                            elif msg.type == WSMsgType.TEXT:
                                print(f"✅ Получено сообщение!")
                                return True
                except asyncio.TimeoutError:
                    print(f"⏱️  Таймаут (5 сек)")
                    return True  # Connection stayed open
                    
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            return False


async def test_method_2(token: str):
    """Method 2: Token in Authorization header."""
    print(f"\n{'='*70}")
    print(f"🧪 МЕТОД 2: Token в Authorization header")
    print(f"{'='*70}")
    
    async with ClientSession() as session:
        headers = {"Authorization": f"Bearer {token}"}
        print(f"Headers: {headers}")
        
        try:
            async with session.ws_connect(WS_ENDPOINT, headers=headers, heartbeat=30) as ws:
                print(f"✅ [{ts()}] Подключено!")
                
                try:
                    async with asyncio.timeout(5):
                        async for msg in ws:
                            print(f"📨 [{ts()}] Тип: {msg.type}, Данные: {msg.data}")
                            if msg.type == WSMsgType.CLOSE:
                                print(f"❌ Закрыто: код={msg.data}")
                                return False
                            elif msg.type == WSMsgType.TEXT:
                                print(f"✅ Получено сообщение!")
                                return True
                except asyncio.TimeoutError:
                    print(f"⏱️  Таймаут (5 сек)")
                    return True
                    
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            return False


async def test_method_3(token: str):
    """Method 3: Send auth message after connection."""
    print(f"\n{'='*70}")
    print(f"🧪 МЕТОД 3: Auth сообщение после подключения")
    print(f"{'='*70}")
    
    async with ClientSession() as session:
        try:
            async with session.ws_connect(WS_ENDPOINT, heartbeat=30) as ws:
                print(f"✅ [{ts()}] Подключено!")
                
                # Send auth message
                auth_msg = json.dumps({"type": "auth", "token": token})
                await ws.send_str(auth_msg)
                print(f"📤 [{ts()}] Отправлено auth сообщение")
                
                try:
                    async with asyncio.timeout(5):
                        async for msg in ws:
                            print(f"📨 [{ts()}] Тип: {msg.type}, Данные: {msg.data[:100] if isinstance(msg.data, str) else msg.data}")
                            if msg.type == WSMsgType.CLOSE:
                                print(f"❌ Закрыто: код={msg.data}")
                                return False
                            elif msg.type == WSMsgType.TEXT:
                                print(f"✅ Получено сообщение!")
                                return True
                except asyncio.TimeoutError:
                    print(f"⏱️  Таймаут (5 сек)")
                    return True
                    
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            return False


async def test_method_4(token: str):
    """Method 4: Subscribe to device updates."""
    print(f"\n{'='*70}")
    print(f"🧪 МЕТОД 4: Subscribe сообщение")
    print(f"{'='*70}")
    
    async with ClientSession() as session:
        ws_url = f"{WS_ENDPOINT}?token={token}"
        
        try:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                print(f"✅ [{ts()}] Подключено!")
                
                # Send subscribe message
                subscribe_msg = json.dumps({"type": "subscribe", "channel": "devices"})
                await ws.send_str(subscribe_msg)
                print(f"📤 [{ts()}] Отправлено subscribe сообщение")
                
                try:
                    async with asyncio.timeout(5):
                        async for msg in ws:
                            print(f"📨 [{ts()}] Тип: {msg.type}")
                            if msg.type == WSMsgType.TEXT:
                                print(f"   Данные: {msg.data[:200]}")
                                return True
                            elif msg.type == WSMsgType.CLOSE:
                                print(f"❌ Закрыто: {msg.data}")
                                return False
                except asyncio.TimeoutError:
                    print(f"⏱️  Таймаут (5 сек)")
                    return True
                    
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            return False


async def main():
    print("="*70)
    print("🔬 DEBUG: Тестирование разных методов WebSocket подключения")
    print("="*70)
    
    email = input("\n📧 Email: ").strip()
    password = input("🔑 Password: ").strip()
    
    token = await login(email, password)
    
    results = {}
    
    # Test all methods
    results["Method 1 (URL)"] = await test_method_1(token)
    await asyncio.sleep(1)
    
    results["Method 2 (Header)"] = await test_method_2(token)
    await asyncio.sleep(1)
    
    results["Method 3 (Auth msg)"] = await test_method_3(token)
    await asyncio.sleep(1)
    
    results["Method 4 (Subscribe)"] = await test_method_4(token)
    
    # Summary
    print(f"\n{'='*70}")
    print(f"📊 ИТОГИ")
    print(f"{'='*70}")
    
    for method, success in results.items():
        status = "✅ Работает" if success else "❌ Не работает"
        print(f"{method}: {status}")
    
    working = [m for m, s in results.items() if s]
    if working:
        print(f"\n✅ Рабочие методы: {', '.join(working)}")
    else:
        print(f"\n❌ Ни один метод не сработал")
        print(f"\n💡 Возможно WebSocket API Atmeex:")
        print(f"   - Не поддерживается для вашего аккаунта")
        print(f"   - Требует специальной подписки")
        print(f"   - Работает только для определенных устройств")
        print(f"   - Использует другой протокол")
        print(f"\n   Рекомендация: используйте HTTP polling (отключите WebSocket)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Прервано")
