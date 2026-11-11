# Хозяйство мака. Запуск из домашней папки: make <цель>
.DEFAULT_GOAL := help
.PHONY: help nespi spi son

help:
	@echo 'make nespi   не спать, даже с закрытой крышкой (спросит пароль sudo)'
	@echo 'make spi     спать как обычно'
	@echo 'make son     что сейчас со сном'

nespi:
	@sudo pmset -a disablesleep 1
	@$(MAKE) -s son

spi:
	@sudo pmset -a disablesleep 0
	@$(MAKE) -s son

son:
	@pmset -g | awk '/SleepDisabled/ {print ($$2 == 1 ? "не спит, даже с закрытой крышкой" : "спит как обычно")}'
