#!/usr/bin/env python3
"""Live WebSocket monitoring for Atmeex Cloud API.

This script connects to WebSocket and listens for 60 seconds.
Use it to test if changes made in Home Assistant trigger WebSocket messages.

Usage:
    1. Run this script in terminal
    2. While it's running, change something in Home Assistant (turn on/off, change speed)
    3. Watch for messages in terminal
"""
import asyncio
import json
import sys
from datetime import datetime
from aiohttp import ClientSession, WSMsgType


API_BASE_URL = "https://api.iot.atmeex.com"
WS_ENDPOINT = "wss://ws.iot.atmeex.com"
LISTEN_DURATION = 60  # seconds


def format_timestamp():
    """Get current timestamp for logging."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def login_and_get_token(email: str, password: str) -> str:
    """Login to Atmeex API and get token."""
    print(f"\n🔐 [{format_timestamp()}] Авторизация: {email}")
    
    async with ClientSession() as session:
        async with session.post(
            f"{API_BASE_URL}/auth/signin",
            json={
                "grant_type": "basic",
                "email": email,
                "password": password,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise Exception(f"Ошибка авторизации {resp.status}: {text[:200]}")
            
            data = await resp.json()
            token = data.get("access_token") or data.get("token")
            
            if not token:
                raise Exception(f"Токен не найден в ответе: {data}")
            
            print(f"✅ [{format_timestamp()}] Авторизация успешна")
            print(f"   Token: {token[:30]}...")
            
            return token


async def listen_websocket(token: str, duration: int = 60):
    """Connect to WebSocket and listen for messages.
    
    Args:
        token: Authentication token
        duration: How long to listen in seconds
    """
    message_count = 0
    
    async with ClientSession() as session:
        ws_url = f"{WS_ENDPOINT}?token={token}"
        
        print(f"\n🔌 [{format_timestamp()}] Подключение к WebSocket...")
        print(f"   URL: {WS_ENDPOINT}")
        
        try:
            async with session.ws_connect(
                ws_url,
                heartbeat=30,
                timeout=10,
            ) as ws:
                print(f"✅ [{format_timestamp()}] WebSocket подключен!")
                print(f"   Статус: {ws.closed}")
                print(f"\n" + "=" * 70)
                print(f"👂 СЛУШАЮ СООБЩЕНИЯ ({duration} секунд)")
                print(f"=" * 70)
                print(f"\n💡 ИНСТРУКЦИЯ:")
                print(f"   1. Оставь этот терминал открытым")
                print(f"   2. Открой Home Assistant в браузере")
                print(f"   3. Измени что-нибудь (включи/выключи бризер, смени скорость)")
                print(f"   4. Смотри сюда - должны появиться сообщения")
                print(f"\n⏱️  Начало: {format_timestamp()}")
                print(f"⏱️  Конец:  {datetime.now().replace(second=(datetime.now().second + duration) % 60).strftime('%H:%M:%S')}")
                print(f"\n" + "-" * 70)
                
                # Try sending auth message (some WebSocket APIs require this)
                print(f"\n🔐 [{format_timestamp()}] Пробую отправить auth сообщение...")
                try:
                    auth_msg = json.dumps({"type": "auth", "token": token})
                    await ws.send_str(auth_msg)
                    print(f"✅ [{format_timestamp()}] Auth сообщение отправлено")
                except Exception as e:
                    print(f"⚠️  [{format_timestamp()}] Не удалось отправить auth: {e}")
                
                # Send initial ping
                await ws.ping()
                print(f"📤 [{format_timestamp()}] Отправлен ping")
                
                # Wait a bit for server response
                await asyncio.sleep(0.5)
                
                # Listen for messages
                try:
                    async with asyncio.timeout(duration):
                        async for msg in ws:
                            timestamp = format_timestamp()
                            
                            if msg.type == WSMsgType.TEXT:
                                message_count += 1
                                print(f"\n" + "=" * 70)
                                print(f"📨 [{timestamp}] СООБЩЕНИЕ #{message_count}")
                                print(f"=" * 70)
                                
                                try:
                                    data = json.loads(msg.data)
                                    print(f"📋 JSON данные:")
                                    print(json.dumps(data, indent=2, ensure_ascii=False))
                                except json.JSONDecodeError:
                                    print(f"📋 Текст данные:")
                                    print(msg.data)
                                
                                print("-" * 70)
                                
                            elif msg.type == WSMsgType.PONG:
                                print(f"🏓 [{timestamp}] Получен pong")
                                
                            elif msg.type == WSMsgType.PING:
                                print(f"🏓 [{timestamp}] Получен ping")
                                
                            elif msg.type == WSMsgType.CLOSE:
                                print(f"\n⚠️  [{timestamp}] Соединение закрыто сервером")
                                print(f"   Код закрытия: {msg.data}")
                                print(f"   Дополнительно: {msg.extra}")
                                if ws.close_code:
                                    print(f"   WebSocket close_code: {ws.close_code}")
                                break
                                
                            elif msg.type == WSMsgType.ERROR:
                                print(f"\n❌ [{timestamp}] Ошибка WebSocket")
                                print(f"   Данные: {msg.data}")
                                break
                                
                except asyncio.TimeoutError:
                    print(f"\n" + "=" * 70)
                    print(f"⏱️  [{format_timestamp()}] Время истекло ({duration} секунд)")
                    print(f"=" * 70)
                
                # Summary
                print(f"\n📊 ИТОГИ:")
                print(f"   Всего получено сообщений: {message_count}")
                
                if message_count == 0:
                    print(f"\n⚠️  СООБЩЕНИЙ НЕ ПОЛУЧЕНО")
                    print(f"\n   Возможные причины:")
                    print(f"   1. WebSocket работает, но сообщения приходят только при изменениях")
                    print(f"   2. Ты не менял(а) ничего в Home Assistant")
                    print(f"   3. WebSocket используется только для определенных событий")
                    print(f"\n   💡 Попробуй:")
                    print(f"   - Запусти скрипт снова")
                    print(f"   - Пока он работает, включи/выключи бризер в HA")
                    print(f"   - Или измени скорость вентилятора")
                else:
                    print(f"\n✅ WebSocket работает и получает обновления!")
                
                await ws.close()
                
        except Exception as e:
            print(f"\n❌ [{format_timestamp()}] Ошибка подключения: {e}")
            raise


async def main():
    print("=" * 70)
    print("🧪 Live WebSocket мониторинг Atmeex Cloud API")
    print("=" * 70)
    
    try:
        # Get credentials
        email = input("\n📧 Email: ").strip()
        password = input("🔑 Password: ").strip()
        
        if not email or not password:
            print("❌ Email и пароль обязательны")
            return
        
        # Login
        token = await login_and_get_token(email, password)
        
        # Listen to WebSocket
        await listen_websocket(token, LISTEN_DURATION)
        
        print(f"\n✅ Тест завершен")
        
    except KeyboardInterrupt:
        print(f"\n\n👋 [{format_timestamp()}] Прервано пользователем")
    except Exception as e:
        print(f"\n❌ [{format_timestamp()}] Ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Завершено")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
