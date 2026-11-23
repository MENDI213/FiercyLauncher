# FiercyLauncher — исправления интерфейса профилей и Modrinth

## Профили и сборки
- После создания профиля установка больше не запускается автоматически: пользователь может сначала сменить версию Minecraft, загрузчик и версию загрузчика.
- Версия загрузчика сохраняется только для соответствующей пары Minecraft + loader; при смене Minecraft/loader список обновляется.
- Вкладка «Каталог модов» скрывает блоки настройки и импорта, оставляя выбор профиля и освобождая основную высоту окна под каталог.
- Действия с файлами/параметрами визуально отделены от установки/запуска.

## Установленные моды
- Список стал выше и прокручивается внутри своей области.
- Рядом с модом отображается checkbox для включения/отключения, иконка и название.
- По названию открывается страница Modrinth (или поиск), ПКМ открывает удаление.
- Поиск и извлечение иконок поддерживают общие пути, Fabric `fabric.mod.json` icon и Forge/NeoForge `logoFile`.

## Modrinth
- При добавлении страниц не перестраивается вся сетка; лишняя повторная компоновка пропускается.
- Описания очищаются от внешних изображений/HTML, которые QTextBrowser отображал как битые значки; галерея остаётся отдельной, изображения открываются в просмотрщике.
- Загрузки изображений управляются главным окном, чтобы закрытие диалога не уничтожало работающие QThread.
- Типы контента вынесены в отдельные кнопки: моды, ресурспаки, шейдеры, готовые сборки.

## NeoForge
- Скачивание официального installer JAR выполняется с таймаутом подключения 25 секунд и тремя повторными попытками; частичный файл не оставляется.
- ConnectTimeout показывает точную официальную ссылку и путь кэширования.
- В настройках профиля NeoForge доступна прямая загрузка через браузер как обходной путь при проблемах Python HTTPS.
- После ручного скачивания JAR можно положить в `.fiercy/installers` и повторить установку.

## Производительность
- Перетаскивание ползунков меняет только значение и подпись; полная регенерация QSS выполняется после отпускания.
- Ограничена повторная перестройка сетки Modrinth, изображения загружаются ограниченным числом потоков.

Проверка: Python compileall и 14 unit-тестов прошли в среде сборки. Полный GUI smoke-test на Windows требует PyQt5 и minecraft-launcher-lib и здесь не выполнялся.

## Ускорение автообновления и обновление до открытия главного окна

- Проверка GitHub Releases перенесена в стартовый bootstrap-экран до импорта и открытия основного окна.
- При включённом автообновлении архив загружается в стартовом окне, затем сверяется SHA-256, передаётся отдельному помощнику установки, а приложение закрывается и запускается заново уже из обновлённых файлов.
- Для больших ZIP используется до 8 параллельных HTTP Range-запросов, если CDN подтверждает поддержку диапазонов; иначе автоматически применяется обычная загрузка с тремя попытками.
- Загрузка используется и в ручной проверке обновлений. Проверенный ZIP кешируется и повторно не скачивается.
- Если GitHub недоступен или обновление не удалось скачать/применить, текущая версия запускается без повреждения установленных файлов.


## Исправления установщика: читаемый ярлык и быстрая загрузка

- Чекбокс создания ярлыка вынесен в отдельную контрастную карточку; увеличены размер текста и индикатора, добавлено понятное пояснение.
- Установщик увеличен, чтобы элементы не выглядели сжатыми.
- Загрузка `FiercyLauncher-win64.zip` в setup переведена на общий проверенный загрузчик `update_download.download_verified_asset`.
- Используются до 8 параллельных HTTP Range-потоков, если GitHub CDN поддерживает Range; в противном случае автоматически остаётся обычная загрузка с повторами.
- Во время скачивания теперь показываются фактическая скорость, объём и режим загрузки.
- SHA-256 проверяется до сохранения архива в кеш; повреждённый пакет не устанавливается.
- PyInstaller setup spec явно включает `update_download`.


## NeoForge installer compatibility

- Prepare `launcher_profiles.json` for the official NeoForge LegacyInstaller when the game directory belongs to a third-party launcher and has no official-launcher profile. Existing files are preserved; malformed files are not overwritten.
- Run the installer with an explicit absolute target and the target directory as its working directory.
- Validate the generated NeoForge version JSON (`id`, `inheritsFrom`, `mainClass`, `libraries`) before reporting the build as installed. Include the installer's log on failures.
- Support both official Maven coordinate families: `net.neoforged:forge` for Minecraft 1.20.1 and `net.neoforged:neoforge` for 1.20.2+ and modern year-based versions such as Minecraft 26.3.
- Add tests for the launcher profile bootstrap, Maven coordinates, stable version filtering and installed profile validation.
- Fix missing `json` import used by Modrinth install metadata persistence.

- For supported classic `1.x` Minecraft releases, installation now delegates to `minecraft_launcher_lib.mod_loader.get_mod_loader("neoforge").install(...)`. This library adapter ensures the vanilla launcher profile exists, runs the official `--install-client` installer, and completes the inherited child-version files through `install_minecraft_version`.
- For Minecraft 26.x (not normalized correctly by the library's current NeoForge adapter) and the special Minecraft 1.20.1 coordinate family, use the explicit Maven-aware fallback, then complete the child profile through minecraft-launcher-lib.
- NeoForge launch remains through `minecraft_launcher_lib.command.get_minecraft_command`, with version metadata validated before enabling Play.


## NeoForge: установку привели к штатному сценарию minecraft-launcher-lib

- Причина ошибки из лога: официальный LegacyInstaller завершался сообщением `There is no minecraft launcher profile` — в `D:\FiercyLauncher` отсутствовал `launcher_profiles.json`. Это не была ошибка Java или скачивания JAR.
- Перед установкой теперь гарантированно создаётся минимальный `launcher_profiles.json` структуры v3 (`profiles`, `settings`, `version: 3`); существующий файл не перезаписывается, повреждённый не затирается.
- Для обычных Minecraft 1.x (кроме старой координатной схемы 1.20.1) используется штатный `minecraft-launcher-lib 8.x` NeoForge adapter: проверка поддержки версии, vanilla dependencies, официальный installer с `--install-client`, а затем установка наследуемой версии и библиотек.
- Для Minecraft 1.20.1 (`net.neoforged:forge`) и современных year-based версий вроде 26.3 оставлен Maven-aware fallback, потому что текущий adapter библиотеки нормализует Minecraft-версии только в формате 1.x. После официального installer зависимости дочерней версии завершаются через `minecraft-launcher-lib`.
- Один и тот же установочный маршрут теперь используется и для обычного создания сборки, и для импорта готового Modrinth/CurseForge modpack.
- Поддержана загрузка списка стабильных NeoForge версий для Minecraft 26.3; предыдущий парсер ошибочно требовал версию Minecraft, начинающуюся с `1.`.
- Перед запуском проверяется сгенерированный version JSON, его `id`, `inheritsFrom`, `mainClass` и `libraries`. Запуск по-прежнему формируется через `minecraft_launcher_lib.command.get_minecraft_command`, как и для Vanilla/Forge/Fabric.

Проверка: Python compileall и 27 unit-тестов прошли. Реальный вызов Java installer и GUI на Windows в среде разработки не запускались.
