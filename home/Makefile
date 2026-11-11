# Хозяйство мака. Запуск из домашней папки: make <цель>
.DEFAULT_GOAL := help
.PHONY: help nespi spi son

help:
	@echo 'make nespi   не спать, даже с закрытой крышкой (спросит пароль sudo)'
	@echo 'make spi     спать как обычно'
	@echo 'make son     что сейчас со сном: SleepDisabled 1 — не спит, 0 — спит'

nespi:
	sudo pmset -a disablesleep 1
	pmset -g | grep SleepDisabled

spi:
	sudo pmset -a disablesleep 0
	pmset -g | grep SleepDisabled

son:
	pmset -g | grep SleepDisabled
