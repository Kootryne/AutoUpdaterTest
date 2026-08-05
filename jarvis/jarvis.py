from jarvis_app.process_controls import apply_patches

apply_patches()

from jarvis_app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
