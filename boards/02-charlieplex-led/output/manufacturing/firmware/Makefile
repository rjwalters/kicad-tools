MCU = attiny85
CC = avr-gcc
OBJCOPY = avr-objcopy
SIZE = avr-size
CFLAGS = -mmcu=$(MCU) -DF_CPU=1000000UL -Os -Wall -Wextra -Werror -std=c11

all: charlieplex.hex

charlieplex.elf: main.c
	$(CC) $(CFLAGS) -o $@ $<
	$(SIZE) $@

charlieplex.hex: charlieplex.elf
	$(OBJCOPY) -O ihex -R .eeprom $< $@

clean:
	rm -f charlieplex.elf charlieplex.hex

.PHONY: all clean
