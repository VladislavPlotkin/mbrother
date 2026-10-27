# Remote Control Tool

[![CI](https://github.com/VladislavPlotkin/mbrother/actions/workflows/ci.yml/badge.svg)](https://github.com/VladislavPlotkin/mbrother/actions)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

<p align="center"><img src="logo.png" alt="Младший брат — логотип" width="320"></p>

[English version](README_en.md)

---

## Описание

**Remote Control** — лёгкая система удалённого управления рабочим столом Linux. Состоит из двух компонентов, каждый на своей машине:
- **Agent** — на удалённой машине (target). Захватывает экран, клавиатуру и мышь, отдаёт их по TCP.
- **Panel** — на управляющем ПК (controller). Показывает видеопоток в браузере или нативном окне, передаёт ввод обратно.

## Возможности

- Просмотр экрана в реальном времени через браузер или отдельное нативное окно (Tkinter) с видео-кодеками MJPEG/H.264/H.265.
- Клавиатура и мышь — полная передача ввода с поддержкой модификаторов и горячих клавиш.
- Буфер обмена — синхронизация clipboards между машинами.
- Wake-on-LAN — пробуждение удалённой машины по сети.
- Мгновенное подключение — без SSH-туннелей и сертификатов.
- Нативный viewer — отдельное окно с тулбаром, зумом, полноэкранным режимом и перетаскиванием.
- Режим блокировки — кнопка «Блокировка» на панели отдельного окна: пока включена, движения и клики мыши не отправляются на удалённую машину (клавиатура продолжает работать); переключается кнопкой или Ctrl+Alt+L.
- Файловый менеджер — просмотр и загрузка файлов с удалённой машины.
- Метрики — разрешение, FPS, пинг в реальном времени.

## Скриншоты

### Веб-интерфейс

Веб-панель управления: браузерный viewer, терминал, файловый менеджер, буфер обмена и метрики в реальном времени.

![Веб-интерфейс](img/web-interface.png)

### Нативный viewer

Отдельное окно с видеопотоком — без заголовка окна ОС, с тулбаром, зумом, перетаскиванием и режимом блокировки мыши (кнопка «Блокировка» / Ctrl+Alt+L).

![Нативный viewer](img/native-viewer.png)

## Установка

### 1. Требования к удалённой машине

- Linux (desktop)
- Python 3.12+
- X11 (`DISPLAY=:0.0`)
- Библиотеки: `Pillow`, `psutil`, `python-xlib`

### 2. Установка на удалённую машину (Agent)

`rc-agent` запускается **только на удалённой машине** — он захватывает её экран через X11.

```bash
git clone https://github.com/VladislavPlotkin/mbrother.git
cd remote-control
pip install -r requirements.txt
```

Создайте конфиг `~/.config/rc/agent.json`:

```json
{
  "host": "0.0.0.0",
  "port": 7777,
  "display": ":0.0",
  "fps": 30,
  "quality": 3,
  "codec": "mjpeg",
  "h264_qp": 23,
  "share": "/tmp/rc-share"
}
```

Запустите agent:

```bash
python3 -m rc.agent
```

или как systemd-сервис:

```bash
cp service/rc-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rc-agent
```

### 3. Установка на управляющий ПК (Panel)

`rc-panel` запускается **только на управляющем ПК** — он показывает видео и отправляет ввод. Агент на этой машине не нужен, там нет удалённого экрана.

```bash
git clone https://github.com/VladislavPlotkin/mbrother.git
cd remote-control
pip install -r requirements.txt
```

Создайте конфиг `~/.config/rc/client.json`:

```json
{
  "agent_host": "192.168.100.16",
  "agent_port": 7777
}
```

Запустите panel:

```bash
python3 -m rc.client
```

или через Makefile:

```bash
make install
systemctl --user enable --now rc-panel
```

### 4. Установка на удалённую машину по SSH

```bash
make install-ssh HOST=USER@REMOTE_IP
```

## Управление сервисами

```bash
# Локально
make install          # установить сервисы
make uninstall        # удалить сервисы

# Удалённо по SSH
make install-ssh HOST=USER@REMOTE
make uninstall-ssh HOST=USER@REMOTE
```

## Архитектура

```
[Panel (порт 8099)] <----TCP----> [Agent (порт 7777)]
      |                                    |
  Веб-интерфейс                  Захват экрана
  Нативный viewer                Ввод мыши/клавиатуры
  Буфер обмена                   Управление процессами
  File Manager                   Wake-on-LAN
```
