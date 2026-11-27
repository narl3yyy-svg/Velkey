import sys

if sys.platform == "darwin":
    # before GTK starts: macOS fixes the menu bar name when the app object is created
    from velkey.macos import set_app_name
    set_app_name("Velkey")

from velkey.app import main  # noqa: E402

sys.exit(main())
