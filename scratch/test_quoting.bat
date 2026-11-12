@echo off
set "TEST_VAR=C:\Path With Spaces\file.db"
echo Original: %TEST_VAR%
start "Test Window" cmd /k "echo Received: %TEST_VAR% && set \"VAR_WITH_SPACES=%TEST_VAR%\" && echo Var set: !VAR_WITH_SPACES! && pause && exit"
