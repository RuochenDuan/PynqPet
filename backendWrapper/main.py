from pynq_pet_gateway.app import app

__all__ = ["app", "main"]


def main() -> None:
    print("Run with: uv run uvicorn main:app --host 0.0.0.0 --port 8000")


if __name__ == "__main__":
    main()
