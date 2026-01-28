#!/usr/bin/env python3
"""Test WebSocket connection to Atmeex Cloud API - Live monitoring mode.

This script connects to WebSocket and listens for real-time updates for 60 seconds.
Perfect for testing if changes made in Home Assistant trigger WebSocket messages.
"""
import asyncio
import json
import sys
from datetime import datetime
from aiohttp import ClientSession, WSMsgType


API_BASE_URL = "https://api.iot.atmeex.com"

# Verified working WebSocket endpoint
WS_ENDPOINT = "wss://ws.iot.atmeex.com"


async def test_websocket_endpoint(url: str, token: str) -> dict:
    """Test a single WebSocket endpoint.
    
    Returns:
        dict with test results
    """
    result = {
        "url": url,
        "success": False,
        "error": None,
        "messages_received": 0,
        "first_message": None,
    }
    
    try:
        async with ClientSession() as session:
            print(f"\n🔌 Тестирую: {url}")
            
            # Try different auth methods
            auth_methods = [
                f"{url}?token={token}",
                f"{url}?auth={token}",
                f"{url}?access_token={token}",
                url,  # No query params, auth in headers
            ]
            
            for ws_url in auth_methods:
                try:
                    headers = {}
                    if ws_url == url:
                        # Try auth in headers
                        headers = {
                            "Authorization": f"Bearer {token}",
                        }
                    
                    print(f"   Попытка подключения: {ws_url[:80]}...")
                    
                    async with session.ws_connect(
                        ws_url,
                        headers=headers,
                        timeout=10,
                        heartbeat=30,
                    ) as ws:
                        print(f"   ✅ Подключено!")
                        result["success"] = True
                        result["url"] = ws_url
                        
                        # Send a ping
                        await ws.ping()
                        print(f"   📤 Отправлен ping")
                        
                        # Listen for messages for 5 seconds
                        print(f"   👂 Слушаю сообщения (5 секунд)...")
                        
                        try:
                            async with asyncio.timeout(5):
                                async for msg in ws:
                                    if msg.type == WSMsgType.TEXT:
                                        result["messages_received"] += 1
                                        data = msg.data
                                        print(f"   📨 Получено сообщение #{result['messages_received']}")
                                        print(f"      Данные: {data[:200]}")
                                        
                                        if result["first_message"] is None:
                                            try:
                                                result["first_message"] = json.loads(data)
                                            except:
                                                result["first_message"] = data
                                        
                                    elif msg.type == WSMsgType.PONG:
                                        print(f"   📨 Получен pong")
                                        
                                    elif msg.type == WSMsgType.CLOSE:
                                        print(f"   ⚠️  Соединение закрыто сервером")
                                        break
                                        
                                    elif msg.type == WSMsgType.ERROR:
                                        print(f"   ❌ Ошибка WebSocket: {msg.data}")
                                        break
                                        
                        except asyncio.TimeoutError:
                            print(f"   ⏱️  Таймаут (5 сек) - сообщений не получено")
                        
                        await ws.close()
                        return result
                        
                except Exception as e:
                    print(f"   ❌ Не удалось подключиться: {e}")
                    continue
            
            result["error"] = "Все методы авторизации не сработали"
            
    except Exception as e:
        result["error"] = str(e)
        print(f"   ❌ Ошибка: {e}")
    
    return result


async def login_and_get_token(email: str, password: str) -> str:
    """Login to Atmeex API and get token."""
    print(f"\n🔐 Авторизация: {email}")
    
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
            
            print(f"✅ Авторизация успешна")
            print(f"   Token: {token[:30]}...")
            
            return token


async def main():
    print("=" * 70)
    print("🧪 Тестирование WebSocket подключения к Atmeex Cloud API")
    print("=" * 70)
    
    # Get credentials
    try:
        email = input("\n📧 Email: ").strip()
        password = input("🔑 Password: ").strip()
        
        if not email or not password:
            print("❌ Email и пароль обязательны")
            return
        
        # Login and get token
        token = await login_and_get_token(email, password)
        
        # Test all endpoints
        print("\n" + "=" * 70)
        print("🔍 Тестирование WebSocket endpoints")
        print("=" * 70)
        
        results = []
        for endpoint in WS_ENDPOINTS:
            result = await test_websocket_endpoint(endpoint, token)
            results.append(result)
            
            if result["success"]:
                print(f"\n✅ УСПЕХ: {result['url']}")
                print(f"   Получено сообщений: {result['messages_received']}")
                if result["first_message"]:
                    print(f"   Первое сообщение:")
                    print(f"   {json.dumps(result['first_message'], indent=2, ensure_ascii=False)[:500]}")
        
        # Summary
        print("\n" + "=" * 70)
        print("📊 ИТОГИ ТЕСТИРОВАНИЯ")
        print("=" * 70)
        
        successful = [r for r in results if r["success"]]
        
        if successful:
            print(f"\n✅ Найдено рабочих endpoints: {len(successful)}")
            for r in successful:
                print(f"\n   URL: {r['url']}")
                print(f"   Сообщений: {r['messages_received']}")
        else:
            print("\n❌ Ни один endpoint не сработал")
            print("\nВозможные причины:")
            print("  1. WebSocket API не поддерживается Atmeex")
            print("  2. Требуется другой метод авторизации")
            print("  3. WebSocket доступен только для определенных устройств")
            print("  4. Endpoint находится на другом домене")
            
            print("\n💡 Рекомендации:")
            print("  - Проверьте документацию Atmeex API")
            print("  - Обратитесь в поддержку Atmeex")
            print("  - Используйте HTTP polling (отключите WebSocket в настройках)")
        
    except KeyboardInterrupt:
        print("\n\n👋 Прервано пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Завершено")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
