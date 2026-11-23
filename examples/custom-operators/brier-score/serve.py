"""Explicit, reviewed import: uploaded code is never dynamically executed by the API."""
import brier_operator  # noqa: F401
from radstudio.operator_serve import main

if __name__ == '__main__':
    main()
