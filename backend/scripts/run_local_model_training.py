"""Run one strict-local model training job in a standalone worker process."""

from app.services.model_training import run_model_training


if __name__ == "__main__":
    run_model_training()
