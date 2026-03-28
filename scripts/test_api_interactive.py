#!/usr/bin/env python3
"""Interactive API testing script for Atmeex Cloud API."""
import asyncio
import json
import sys
from aiohttp import ClientSession


API_BASE_URL = "https://api.iot.atmeex.com"


class AtmeexApiTester:
    def __init__(self):
        self.session = None
        self.token = None
        self.token_type = "Bearer"
        self.devices = []
    
    async def __aenter__(self):
        self.session = ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    def _headers(self):
        if self.token:
            return {
                "Authorization": f"{self.token_type} {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    
    async def login(self, email: str, password: str):
        """Авторизация в API."""
        print(f"\n🔐 Авторизация: {email}")
        
        async with self.session.post(
            f"{API_BASE_URL}/auth/signin",
            json={
                "grant_type": "basic",
                "email": email,
                "password": password,
            },
            headers=self._headers(),
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                print(f"❌ Ошибка авторизации {resp.status}: {text[:200]}")
                return False
            
            data = await resp.json()
            self.token = data.get("access_token") or data.get("token")
            self.token_type = data.get("token_type", "Bearer")
            
            print(f"✅ Авторизация успешна")
            print(f"   Token: {self.token[:20]}...")
            return True
    
    async def get_devices(self):
        """Получить список устройств."""
        print(f"\n📱 Получение списка устройств...")
        
        async with self.session.get(
            f"{API_BASE_URL}/devices",
            headers=self._headers(),
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                print(f"❌ Ошибка {resp.status}: {text[:200]}")
                return []
            
            data = await resp.json()
            
            if isinstance(data, dict) and "items" in data:
                self.devices = data["items"]
            elif isinstance(data, list):
                self.devices = data
            else:
                print(f"⚠️  Неожиданный формат ответа: {type(data)}")
                self.devices = []
            
            print(f"✅ Найдено устройств: {len(self.devices)}")
            
            for i, dev in enumerate(self.devices):
                print(f"\n   [{i}] {dev.get('name', 'Unknown')}")
                print(f"       ID: {dev.get('id')}")
                print(f"       Model: {dev.get('model')}")
                print(f"       Online: {dev.get('online')}")
                
                # Показываем текущее состояние
                cond = dev.get('condition', {})
                settings = dev.get('settings', {})
                
                pwr = cond.get('pwr_on', settings.get('u_pwr_on'))
                fan_speed = cond.get('fan_speed', settings.get('u_fan_speed'))
                temp = cond.get('temp_room')
                
                print(f"       Power: {pwr}")
                print(f"       Fan Speed: {fan_speed}")
                print(f"       Temp: {temp/10 if temp else None}°C")
            
            return self.devices
    
    async def get_device(self, device_id):
        """Получить детальную информацию об устройстве."""
        print(f"\n🔍 Получение информации об устройстве {device_id}...")
        
        async with self.session.get(
            f"{API_BASE_URL}/devices/{device_id}",
            headers=self._headers(),
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                print(f"❌ Ошибка {resp.status}: {text[:200]}")
                return None
            
            data = await resp.json()
            
            print(f"✅ Устройство получено")
            print(f"\n📊 Полные данные:")
            print(json.dumps(data, indent=2, ensure_ascii=False))
            
            return data
    
    async def set_fan_speed(self, device_id, speed: int):
        """Установить скорость вентилятора."""
        print(f"\n⚙️  Установка скорости вентилятора: {speed}")
        
        body = {"u_fan_speed": int(speed)}
        
        async with self.session.put(
            f"{API_BASE_URL}/devices/{device_id}/params",
            json=body,
            headers=self._headers(),
        ) as resp:
            status = resp.status
            
            if status >= 400:
                text = await resp.text()
                print(f"❌ Ошибка {status}: {text[:200]}")
                return None
            
            try:
                data = await resp.json()
            except:
                data = await resp.text()
            
            print(f"✅ Команда отправлена (status={status})")
            print(f"   Ответ: {json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, dict) else data}")
            
            # Подождем немного и получим обновленное состояние
            await asyncio.sleep(1)
            updated = await self.get_device(device_id)
            
            if updated:
                cond = updated.get('condition', {})
                settings = updated.get('settings', {})
                
                actual_speed = cond.get('fan_speed', settings.get('u_fan_speed'))
                
                print(f"\n📈 После установки скорости {speed}:")
                print(f"   condition.fan_speed = {cond.get('fan_speed')}")
                print(f"   settings.u_fan_speed = {settings.get('u_fan_speed')}")
                print(f"   Фактическая скорость: {actual_speed}")
                
                if actual_speed != speed:
                    print(f"   ⚠️  ВНИМАНИЕ: Запросили {speed}, получили {actual_speed}")
            
            return data
    
    async def set_power(self, device_id, on: bool):
        """Включить/выключить устройство."""
        print(f"\n⚡ {'Включение' if on else 'Выключение'} устройства...")
        
        body = {"u_pwr_on": bool(on)}
        
        async with self.session.put(
            f"{API_BASE_URL}/devices/{device_id}/params",
            json=body,
            headers=self._headers(),
        ) as resp:
            status = resp.status
            
            if status >= 400:
                text = await resp.text()
                print(f"❌ Ошибка {status}: {text[:200]}")
                return None
            
            try:
                data = await resp.json()
            except:
                data = await resp.text()
            
            print(f"✅ Команда отправлена (status={status})")
            
            return data


async def main():
    print("=" * 60)
    print("🧪 Интерактивное тестирование Atmeex Cloud API")
    print("=" * 60)
    
    # Запрашиваем учетные данные
    email = input("\n📧 Email: ").strip()
    password = input("🔑 Password: ").strip()
    
    if not email or not password:
        print("❌ Email и пароль обязательны")
        return
    
    async with AtmeexApiTester() as tester:
        # Авторизация
        if not await tester.login(email, password):
            return
        
        # Получаем устройства
        devices = await tester.get_devices()
        
        if not devices:
            print("\n❌ Устройства не найдены")
            return
        
        # Выбираем устройство
        if len(devices) == 1:
            device_idx = 0
        else:
            device_idx = int(input(f"\n🎯 Выберите устройство [0-{len(devices)-1}]: ").strip() or "0")
        
        device = devices[device_idx]
        device_id = device['id']
        
        print(f"\n✅ Выбрано устройство: {device.get('name')} (ID: {device_id})")
        
        # Получаем детальную информацию
        await tester.get_device(device_id)
        
        # Интерактивное меню
        while True:
            print("\n" + "=" * 60)
            print("Команды:")
            print("  1-7: Установить скорость вентилятора")
            print("  on:  Включить устройство")
            print("  off: Выключить устройство")
            print("  info: Получить текущее состояние")
            print("  test: Протестировать все скорости 1-7")
            print("  q: Выход")
            print("=" * 60)
            
            cmd = input("\n> ").strip().lower()
            
            if cmd == 'q':
                break
            elif cmd == 'on':
                await tester.set_power(device_id, True)
            elif cmd == 'off':
                await tester.set_power(device_id, False)
            elif cmd == 'info':
                await tester.get_device(device_id)
            elif cmd == 'test':
                print("\n🧪 Тестирование всех скоростей 1-7...")
                for speed in range(1, 8):
                    await tester.set_fan_speed(device_id, speed)
                    await asyncio.sleep(2)
            elif cmd.isdigit() and 1 <= int(cmd) <= 7:
                await tester.set_fan_speed(device_id, int(cmd))
            else:
                print("❌ Неизвестная команда")
    
    print("\n👋 Завершено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Прервано пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
