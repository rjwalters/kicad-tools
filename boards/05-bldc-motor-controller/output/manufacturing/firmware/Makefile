MCU = atmega328p
CC = avr-gcc
OBJCOPY = avr-objcopy
CFLAGS = -mmcu=$(MCU) -Os -std=c99 -Wall -Wextra -Werror
all: bldc.hex
bldc.elf: main.c commutation.h
	$(CC) $(CFLAGS) -o $@ main.c
bldc.hex: bldc.elf
	$(OBJCOPY) -O ihex -R .eeprom $< $@
clean:
	rm -f bldc.elf bldc.hex
.PHONY: all clean
