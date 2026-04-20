"""Entry point: ``python -m peppar_mon`` runs the app."""

from peppar_mon.app import PepparMonApp


def main() -> None:
    PepparMonApp().run()


if __name__ == "__main__":
    main()
