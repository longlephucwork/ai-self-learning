@echo off
rem learning-harness usage wrapper for 'csv-to-json' on Windows: runs bin\csv-to-json with Python.
if defined LEARNING_HARNESS_PYTHON (
  "%LEARNING_HARNESS_PYTHON%" "%~dp0csv-to-json" %*
) else (
  python "%~dp0csv-to-json" %*
)
exit /b %ERRORLEVEL%
