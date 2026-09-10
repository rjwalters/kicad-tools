/* ATtiny85 charlieplex demo, revision B. Factory 8 MHz RC / CKDIV8 = 1 MHz.
 * A=PB3, B=PB4, C=PB0, D=PB1. PB2 is ISP SCK; PB5 remains RESET.
 * One LED is active at a time. Break-before-make prevents ghosting.
 */
#ifndef F_CPU
#define F_CPU 1000000UL
#endif
#include <avr/io.h>
#include <stdint.h>
#include <util/delay.h>

static const uint8_t lines[] = {PB3, PB4, PB0, PB1};
static const uint8_t pairs[9][2] = {
    {0, 1}, {1, 0}, {0, 2}, {2, 0}, {0, 3},
    {3, 0}, {1, 2}, {2, 1}, {1, 3}
};

static void blank(void) {
    DDRB = 0;   /* All matrix pins high impedance before changing polarity. */
    PORTB = 0;  /* No pullups on inactive matrix pins. */
}

int main(void) {
    blank();
    /* Disable analog inputs/peripherals we do not use; GPIO remains digital. */
    ADCSRA = 0;
    ACSR = _BV(ACD);
    for (;;) {
        for (uint8_t led = 0; led < 9; ++led) {
            blank();
            PORTB = _BV(lines[pairs[led][0]]);
            DDRB = _BV(lines[pairs[led][0]]) | _BV(lines[pairs[led][1]]);
            _delay_ms(150);
        }
    }
}
