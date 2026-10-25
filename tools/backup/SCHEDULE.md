# Расписание резервных копий

Скрипт: `bash tools/backup/backup.sh` (хранит 14 дней, `RETENTION_DAYS` меняет срок, `BACKUP_DIR` — каталог). Запускайте раз в сутки ночью.
Время в расписании — местное время сервера (для Минска это UTC+3: проверьте часовой пояс сервера/ОС, иначе «3 часа ночи» окажется другим).

Режим «одна копия»: с `BACKUP_OVERWRITE=1` скрипт каждый запуск заменяет `backups/bbot_latest.dump` свежей копией (без истории и ротации).
Замена атомарная: прежний файл заменяется, только когда новая копия создана и прочитана `pg_restore`; при ошибке прежняя копия остаётся.
Недостаток: если ночью в базе уже была ошибка (случайное удаление данных), она попадёт в единственную копию. Историю даёт запуск без этой переменной.
Проверка восстановления — раз в неделю: `restore.sh` на самой свежей копии. Копии выносите на другой диск или сервер.

## Linux (cron)

`crontab -e`, пути замените на свои:

```cron
0 3 * * *  cd /opt/bbot && BACKUP_OVERWRITE=1 bash tools/backup/backup.sh >> backups/backup.log 2>&1
45 3 * * 0  cd /opt/bbot && bash tools/backup/restore.sh --dump latest --db restore_check_weekly >> backups/restore_check.log 2>&1
```

Проверка: `crontab -l`; на следующий день `tail backups/backup.log` — строка «Готово: ... оглавление прочитано».

## Windows (Планировщик заданий)

Docker Desktop должен быть запущен. В **cmd** или PowerShell (не в Git Bash — там слеши в параметрах искажаются), путь `C:\bbot` замените на свой:

```bat
schtasks /Create /TN "bbot-backup" /SC DAILY /ST 03:00 /TR "\"C:\Program Files\Git\bin\bash.exe\" -lc \"cd /c/bbot && BACKUP_OVERWRITE=1 bash tools/backup/backup.sh >> backups/backup.log 2>&1\""
schtasks /Create /TN "bbot-restore-check" /SC WEEKLY /D SUN /ST 03:45 /TR "\"C:\Program Files\Git\bin\bash.exe\" -lc \"cd /c/bbot && bash tools/backup/restore.sh --dump latest --db restore_check_weekly >> backups/restore_check.log 2>&1\""
```

Проверка: `schtasks /Run /TN "bbot-backup"`, затем `type C:\bbot\backups\backup.log`.
Если задание должно работать без входа пользователя в систему — откройте его в «Планировщике заданий» и выберите
«Выполнять вне зависимости от регистрации пользователя» (потребуется пароль учётной записи Windows).
