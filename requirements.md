# Project Requirements

This document outlines the Python package dependencies required for the **PiPhone** project.

## Core Dependencies

| Package | Minimum Version | Purpose |
|---------|----------------|---------|
| `customtkinter` | `5.2.0` | Modern UI toolkit for Tkinter applications |
| `packaging` | `23.0` | Utilities for version handling and package metadata |
| `pyinstaller` | `6.0.0` | Build a standalone executable for distribution |
| `mpmath` | `1.3.0` | Arbitrary-precision arithmetic & Chudnovsky $\pi$ engine |
| `gmpy2` | `2.1.5` | Compiled C big-integer math backend accelerating $\pi$ calculations 10x–15x |
| `pytest` | `8.0.0` | Test runner and test suite execution |

These dependencies are listed in the [`requirements.txt`](file:///c:/Users/busht/OneDrive/Dev/PiPhone/requirements.txt) file and can be installed with:

```sh
pip install -r requirements.txt
```

Alternatively, you can install them directly:

```sh
pip install customtkinter packaging pyinstaller mpmath gmpy2 pytest
```

## Development Tools (Optional)

You may also want to install development utilities such as:

- `black` – for code formatting
- `mypy` – for type checking

These are not required for the core functionality but can help maintain code quality.

---
*Generated on 2026-10-17.*

