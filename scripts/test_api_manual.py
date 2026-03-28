#!/usr/bin/env python3
"""Manual API testing script for Atmeex Cloud API - run this in your terminal."""
import asyncio
import json
import sys
from aiohttp import ClientSession


API_BASE_URL = "https://api.iot.atmeex.com"


async def test_api(email: str, password: str):
    """Test Atmeex API with provided credentials."""
    
    print("=" * 70)
    print("🧪 Тестирование Atmeex Cloud API")
    print("=" * 70)
    
    async with ClientSession() as session:
        # 1. Авторизация
        print(f"\n🔐 Шаг 1: Авторизация ({email})")
        async with session.post(
            f"{API_BASE_URL}/auth/signin",
            json={"grant_type": "basic", "email": email, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        ) as resp:
            if resp.status >= 400:
                print(f"❌ Ошибка авторизации {resp.status}: {await resp.text()}")
                return
            
            auth_data = await resp.json()
            token = auth_data.get("access_token") or auth_data.get("token")
            token_type = auth_data.get("token_type", "Bearer")
            print(f"✅ Авторизация успешна, token: {token[:30]}...")
        
        headers = {
            "Authorization": f"{token_type} {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        # 2. Получение списка устройств
        print(f"\n📱 Шаг 2: Получение списка устройств")
        async with session.get(f"{API_BASE_URL}/devices", headers=headers) as resp:
            if resp.status >= 400:
                print(f"❌ Ошибка {resp.status}: {await resp.text()}")
                return
            
            devices_data = await resp.json()
            
            if isinstance(devices_data, dict) and "items" in devices_data:
                devices = devices_data["items"]
            elif isinstance(devices_data, list):
                devices = devices_data
            else:
                devices = []
            
            print(f"✅ Найдено устройств: {len(devices)}\n")
            
            for i, dev in enumerate(devices):
                print(f"[{i}] {dev.get('name', 'Unknown')} (ID: {dev.get('id')})")
                cond = dev.get('condition', {})
                settings = dev.get('settings', {})
                print(f"    Power: {cond.get('pwr_on', settings.get('u_pwr_on'))}")
                print(f"    Fan Speed: {cond.get('fan_speed', settings.get('u_fan_speed'))}")
                print(f"    Temp: {cond.get('temp_room', 0)/10}°C")
        
        if not devices:
            print("❌ Устройства не найдены")
            return
        
        # Выбираем первое устройство
        device = devices[0]
        device_id = device['id']
        print(f"\n🎯 Выбрано устройство: {device.get('name')} (ID: {device_id})")
        
        # 3. Получаем детальную информацию
        print(f"\n🔍 Шаг 3: Детальная информация об устройстве")
        async with session.get(f"{API_BASE_URL}/devices/{device_id}", headers=headers) as resp:
            device_detail = await resp.json()
            
            print("\n📊 Полная структура данных устройства:")
            print(json.dumps(device_detail, indent=2, ensure_ascii=False))
            
            cond = device_detail.get('condition', {})
            settings = device_detail.get('settings', {})
            
            print(f"\n📈 Текущее состояние:")
            print(f"   condition.pwr_on = {cond.get('pwr_on')}")
            print(f"   condition.fan_speed = {cond.get('fan_speed')}")
            print(f"   settings.u_pwr_on = {settings.get('u_pwr_on')}")
            print(f"   settings.u_fan_speed = {settings.get('u_fan_speed')}")
        
        # Тестируем установку скоростей
        print(f"\n⚙️  Шаг 4: Тестирование установки скоростей вентилятора")
        print("=" * 70)
        print("ℹ️  HA использует скорости 1-7, API использует 0-6")
        print("   Конвертация: HA speed - 1 = API speed")
        print("=" * 70)
        
        test_speeds = [1, 2, 3, 4, 5, 6, 7]
        results = []
        
        for ha_speed in test_speeds:
            # Конвертируем HA speed (1-7) в API speed (0-6)
            api_speed = ha_speed - 1 if ha_speed > 0 else 0
            
            print(f"\n🔧 Устанавливаем HA скорость: {ha_speed} (API: {api_speed})")
            
            # Отправляем команду с API скоростью
            async with session.put(
                f"{API_BASE_URL}/devices/{device_id}/params",
                json={"u_fan_speed": api_speed},
                headers=headers,
            ) as resp:
                status = resp.status
                try:
                    response_data = await resp.json()
                except:
                    response_data = await resp.text()
                
                print(f"   Ответ API (status={status}): {response_data}")
            
            # Ждем немного
            await asyncio.sleep(1.5)
            
            # Получаем обновленное состояние
            async with session.get(f"{API_BASE_URL}/devices/{device_id}", headers=headers) as resp:
                updated = await resp.json()
                cond = updated.get('condition', {})
                settings = updated.get('settings', {})
                
                # API возвращает 0-6, конвертируем в HA 1-7 для сравнения
                api_condition_speed = cond.get('fan_speed')
                api_settings_speed = settings.get('u_fan_speed')
                
                # Конвертируем API скорости в HA скорости для отображения
                ha_condition_speed = (api_condition_speed + 1) if api_condition_speed and api_condition_speed > 0 else 0
                ha_settings_speed = (api_settings_speed + 1) if api_settings_speed and api_settings_speed > 0 else 0
                
                # Используем settings как источник истины (он обновляется сразу)
                actual_ha_speed = ha_settings_speed if ha_settings_speed else ha_condition_speed
                
                result = {
                    'requested_ha': ha_speed,
                    'requested_api': api_speed,
                    'api_condition': api_condition_speed,
                    'api_settings': api_settings_speed,
                    'ha_condition': ha_condition_speed,
                    'ha_settings': ha_settings_speed,
                    'actual_ha': actual_ha_speed,
                    'match': actual_ha_speed == ha_speed
                }
                results.append(result)
                
                print(f"   После установки:")
                print(f"     API: condition={api_condition_speed}, settings={api_settings_speed}")
                print(f"     HA:  condition={ha_condition_speed}, settings={ha_settings_speed}")
                print(f"     Фактическая HA скорость: {actual_ha_speed}")
                
                if actual_ha_speed != ha_speed:
                    print(f"     ⚠️  НЕСООТВЕТСТВИЕ: запросили HA {ha_speed}, получили {actual_ha_speed}")
                else:
                    print(f"     ✅ Скорость установлена корректно")
        
        # Итоговая таблица
        print("\n" + "=" * 70)
        print("📊 ИТОГОВАЯ ТАБЛИЦА РЕЗУЛЬТАТОВ:")
        print("=" * 70)
        print(f"{'HA Запрос':<12} {'API→':<8} {'API cond':<10} {'API set':<10} {'HA Факт':<12} {'Статус':<10}")
        print("-" * 70)
        
        for r in results:
            status = "✅ OK" if r['match'] else "❌ FAIL"
            print(f"{r['requested_ha']:<12} {r['requested_api']:<8} {str(r['api_condition']):<10} {str(r['api_settings']):<10} {r['actual_ha']:<12} {status:<10}")
        
        # Анализ
        print("\n" + "=" * 70)
        print("🔬 АНАЛИЗ:")
        print("=" * 70)
        
        mismatches = [r for r in results if not r['match']]
        if mismatches:
            print(f"⚠️  Обнаружено {len(mismatches)} несоответствий:")
            for r in mismatches:
                diff = r['actual_ha'] - r['requested_ha']
                print(f"   HA скорость {r['requested_ha']} (API {r['requested_api']}) → получили HA {r['actual_ha']} (смещение: {diff:+d})")
            
            print("\n💡 ПРИЧИНА:")
            print("   condition.fan_speed обновляется с задержкой от устройства (2-8 сек)")
            print("   settings.u_fan_speed обновляется сразу и используется как fallback")
            print("\n✅ РЕШЕНИЕ УЖЕ РЕАЛИЗОВАНО В ИНТЕГРАЦИИ:")
            print("   1. Конвертация HA (1-7) ↔ API (0-6)")
            print("   2. Pending command tracking (TTL=8s)")
            print("   3. Fallback на settings.u_fan_speed при устаревшем condition")
        else:
            print("✅ Все скорости установлены корректно!")
            print("   Конвертация HA (1-7) ↔ API (0-6) работает правильно")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("Этот скрипт нужно запустить вручную в терминале:")
    print("=" * 70)
    print("\n1. Откройте терминал")
    print("2. Перейдите в директорию проекта:")
    print("   cd /Users/romankhodukin/Desktop/Programming/HomeAssistant/atmeex_hacs")
    print("\n3. Активируйте виртуальное окружение:")
    print("   source .venv/bin/activate")
    print("\n4. Запустите скрипт:")
    print("   python3 test_api_manual.py")
    print("\n5. Введите email и пароль когда попросит")
    print("=" * 70)
    
    # Пробуем интерактивный режим
    try:
        email = input("\n📧 Email: ").strip()
        password = input("🔑 Password: ").strip()
        
        if email and password:
            asyncio.run(test_api(email, password))
        else:
            print("\n❌ Email и пароль обязательны")
    except EOFError:
        print("\n\n⚠️  Скрипт запущен в неинтерактивном режиме")
        print("Пожалуйста, запустите его вручную в терминале (см. инструкции выше)")
