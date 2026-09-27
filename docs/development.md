# Локальная разработка GTag Display

[Описание проекта](../README.md) · [Проверки](testing.md) · [История версий](../CHANGELOG.md)

Для обычной установки используйте [ESPHome Device Builder](esphome-flashing.md)
и [мастер YAML в HA](firmware-wizard.md). Ниже — работа с исходниками репозитория.

## Конфигурации

Все профили используют общий компонент
[`gtag_display`](../config/esphome/components/gtag_display).
Публичные YAML загружают закреплённый выпуск с GitHub; локальные — соседний
каталог `components`. Аппаратные параметры задаются явно.

| Профиль | Публичный YAML | Локальный YAML |
|---|---|---|
| Bluetooth | [gtag-ble.yaml](../config/esphome/gtag-ble.yaml) | [nrf-gtag-display.yaml](../config/esphome/nrf-gtag-display.yaml) |
| Zigbee2MQTT | [gtag-zigbee.yaml](../config/esphome/gtag-zigbee.yaml) | [nrf-gtag-zigbee.yaml](../config/esphome/nrf-gtag-zigbee.yaml) |
| Super52840, Zigbee, без АЦП | [gtag-super52840-zigbee.yaml](../config/esphome/gtag-super52840-zigbee.yaml) | [nrf-gtag-super52840-zigbee.yaml](../config/esphome/nrf-gtag-super52840-zigbee.yaml) |
| ESP32-C3, Wi-Fi | [gtag-esp32-c3-wifi.yaml](../config/esphome/gtag-esp32-c3-wifi.yaml) | [esp32-gtag-display.yaml](../config/esphome/esp32-gtag-display.yaml) |

[GPIO и загрузчики](pin-remapping.md) · [Аккумулятор](battery.md) ·
[Пример полной конфигурации](device-configuration.md).

## Сборка nRF52840

Нужен Python 3.12+. Первая сборка скачивает SDK; используемое окружение —
ESPHome 2026.9.0 и nRF Connect SDK 2.9.2.

```sh
python3 -m venv .venv
.venv/bin/pip install esphome==2026.9.0
.venv/bin/esphome config config/esphome/nrf-gtag-display.yaml
.venv/bin/esphome compile config/esphome/nrf-gtag-display.yaml
```

Для Zigbee замените имя YAML на соответствующее в таблице. UF2 для BLE:

```text
config/esphome/.esphome/build/nrf-gtag-display/.pioenvs/nrf-gtag-display/zephyr/zephyr.uf2
```

В основной конфигурации USB-порт приложения отключён. Вход в загрузчик —
аппаратным Reset, обычно двойным нажатием. Профиль загрузчика должен
соответствовать плате. [Запись UF2](esphome-flashing.md).

## Установка и упаковка интеграции

Скопируйте `custom_components/gtag_ble_test` целиком, включая `fonts`,
в `/config/custom_components/` и перезапустите HA. Технический домен
`gtag_ble_test` сохраняется для совместимости сущностей и настроек.

```sh
python3 scripts/package.py
```

Архив `dist/gtag-ha-integration.zip` распаковывается в `/config/` HA.
Скрипт включает шрифты и формирует `SHA256SUMS` для интеграции и имеющегося
BLE UF2. Для полного комплекта обновления суммы нужно сформировать для всех файлов.

Для упаковки прошивки используйте `scripts/package_firmware.py --help`,
для исходников Wi-Fi — `scripts/package_wifi.py --help`.
Проверка UF2 учитывает ограничение загрузчика `0xAD000`; одной успешной
линковки недостаточно. [Результаты сборок и проверки](testing.md).

## Ожидание и диагностика BLE

Используется System ON idle: CPU останавливается между работой потоков
и прерываниями, сохраняя RAM, GPIO и обслуживание Bluetooth. Компонент LCD
просыпается по событиям и таймерам; постоянного опроса нет.

- Главный цикл ESPHome просыпается раз в секунду для служебной работы;
  события BLE и таймеры могут разбудить его раньше.
- Интервал рекламы по умолчанию — одна секунда. Он настраивается через
  `advertising_interval`; мощность — через `tx_power`.
- После COMMIT клиент читает STATUS и отключается. Во время записи в LCD
  реклама приостановлена и возобновляется после завершения.
- USB CDC, UART и логирование в основной конфигурации отключены.
  Для диагностики можно включить `logger`; это влияет на энергопотребление.
- После RESET выдерживается 50 мс; стартовая пауза — 3 с.
- `boot_test_pattern` выбирает заставку или диагностический рисунок:
  `logo`, `none`, `white`, `black`, `checkerboard`, `stripes`.

System OFF не используется, поскольку устройство должно принимать
подключения. LCD работает от штатного DisplayCLK/S1.
Переключатель HA **Virtual LED** меняет флаг в RAM для проверки GATT,
а не физический светодиод. Он разделяет блокировку с передачей кадров.

[Протокол и кодеки](firmware-compatibility.md) ·
[Протокол Zigbee](zigbee-protocol.md) · [Диагностика HA](device-diagnostics.md).

## Архивные материалы

Первоначальные самостоятельные прошивки и тестовые компоненты можно извлечь
из коммита `faeddc2`:

```sh
git archive --format=zip --output=/tmp/gtag-prototypes.zip faeddc2
```

- [Описание ценника](../gtag-info.jpg).
- [Запись анализатора ESP32](../esp32-diagram/ESP32-gtag.sal)
  и [скриншот](../esp32-diagram/Esp32-gtag.png).
- [Исследование G-Tag 6](https://gist.github.com/bttrem/30eefce2549c1a873f382b5cbdcb3541).
