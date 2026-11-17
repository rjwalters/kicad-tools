/* Board05 revision B. Bench firmware for a small sensored BLDC motor.
 * Factory internal RC clock; runtime divide-by-one gives 8 MHz.
 * RUN must be released after power-on and after every latched fault.
 * Maximum PWM duty 63/256; current comparator trip nominally 1 A.
 * Physical motor/thermal validation is required before increasing limits.
 */
#define F_CPU 8000000UL
#include <avr/io.h>
#include <avr/interrupt.h>
#include <avr/wdt.h>
#include <util/delay.h>
#include "commutation.h"

#define EN_D (_BV(PD4)|_BV(PD7))
#define PWM_D (_BV(PD3)|_BV(PD5)|_BV(PD6))
static volatile uint8_t tripped;

/* Shut down independent of PWM phase. Safe also inside the trip interrupt. */
static void disable_bridge(void) {
    PORTD &= ~EN_D;
    PORTB &= ~(_BV(PB0)|_BV(PB1));
}
ISR(INT0_vect) { disable_bridge(); tripped=1; }

static void drive(uint8_t hall,uint8_t duty) {
    uint8_t saved=SREG; cli();
    PORTD &= ~EN_D; PORTB &= ~_BV(PB0);
    TCCR0A=_BV(WGM00)|_BV(WGM01); TCCR2A=_BV(WGM20)|_BV(WGM21);
    PORTD &= ~PWM_D;
    if (!tripped && valid_hall(hall) && duty) {
        uint8_t high=high_phase[hall],low=low_phase[hall];
        OCR0A=duty; OCR0B=duty; OCR2B=duty;
        if (high==0) TCCR2A|=_BV(COM2B1);
        if (high==1) TCCR0A|=_BV(COM0B1);
        if (high==2) TCCR0A|=_BV(COM0A1);
        _delay_us(2);
        if (high==0 || low==0) PORTD|=_BV(PD4);
        if (high==1 || low==1) PORTD|=_BV(PD7);
        if (high==2 || low==2) PORTB|=_BV(PB0);
    }
    SREG=saved;
}
static uint8_t read_speed(void) {
    ADCSRA|=_BV(ADSC); while (ADCSRA&_BV(ADSC)) {}
    uint16_t value=ADC;
    return value<64 ? 0 : (uint8_t)(value>>4); /* 0..63 */
}
int main(void) {
    cli(); MCUSR=0; wdt_disable(); CLKPR=_BV(CLKPCE); CLKPR=0;
    PORTD=0; PORTB=0; PORTC=0;
    DDRD=EN_D|PWM_D|_BV(PD0); DDRB=_BV(PB0)|_BV(PB1); DDRC=0;
    disable_bridge();
    TCCR0A=_BV(WGM00)|_BV(WGM01); TCCR0B=_BV(CS00);
    TCCR2A=_BV(WGM20)|_BV(WGM21); TCCR2B=_BV(CS20);
    ADMUX=_BV(REFS0)|3; ADCSRA=_BV(ADEN)|_BV(ADPS2)|_BV(ADPS1);
    DIDR0=_BV(ADC3D); ACSR=_BV(ACD);
    EICRA=_BV(ISC01); /* Falling edge on active-low current trip. */
    sei(); wdt_enable(WDTO_250MS);
    uint8_t released=0,running=0,last=0,duty=0,old_duty=255,divider=0;
    uint16_t stationary=0;
    for (;;) {
        wdt_reset();
        if (PINC&_BV(PC5)) { /* Open RUN input. */
            disable_bridge(); EIMSK=0; running=0; tripped=0;
            released=1; PORTD&=~_BV(PD0);
        } else if (!running && released && !tripped) {
            released=0; PORTB|=_BV(PB1); _delay_ms(5);
            uint8_t hall=PINC&7;
            if (!(PINB&_BV(PB2)) || !(PIND&_BV(PD2)) || !valid_hall(hall)) {
                tripped=1; disable_bridge();
            } else {
                EIFR=_BV(INTF0); EIMSK=_BV(INT0);
                running=1; last=hall; stationary=0; old_duty=255;
            }
        }
        if (running && !tripped) {
            uint8_t hall=PINC&7;
            if (!(PINB&_BV(PB2)) || !valid_hall(hall)
                || (hall!=last && hall!=next_hall[last])) tripped=1;
            if (++divider==16) { divider=0; duty=read_speed(); }
            if (hall!=last || !duty) stationary=0;
            else if (++stationary>=2000) tripped=1; /* ~200 ms stalled. */
            if (tripped) disable_bridge();
            else if (hall!=last || duty!=old_duty) drive(hall,duty);
            last=hall; old_duty=duty;
        }
        if (tripped) { disable_bridge(); PORTD|=_BV(PD0); }
        _delay_us(100);
    }
}
