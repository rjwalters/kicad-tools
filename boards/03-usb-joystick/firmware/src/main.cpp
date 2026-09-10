#include <Arduino.h>
#include <Joystick.h>
#include <avr/io.h>

// Read silicon ports directly: the board does not use Leonardo header mapping.
// U1.41 PF0/ADC0 = X, U1.40 PF1/ADC1 = Y.
// U1.18/19/20/21/25 PD0..4 = BTN1..4 and joystick pushbutton.
static Joystick_ joystick(0x03, JOYSTICK_TYPE_JOYSTICK, 5, 0,
                          true, true, false, false, false, false,
                          false, false, false, false, false);
static uint8_t debounce[5] = {};
static uint8_t pressed = 0;

static uint16_t convertADC() {
    ADCSRA |= _BV(ADSC);
    while (ADCSRA & _BV(ADSC)) {}
    return ADC;
}

static uint16_t readAxis(uint8_t channel) {
    ADMUX = _BV(REFS0) | channel; // AVCC reference; right-adjusted 10-bit result.
    ADCSRB &= ~_BV(MUX5);
    (void)convertADC(); // Discard after mux change so the sample capacitor settles.
    return convertADC();
}

static_assert(F_CPU == 8000000UL, "Board revision B uses an 8 MHz crystal");

void setup() {
    DDRD &= ~0x1f;
    PORTD &= ~0x1f; // The PCB provides external 10k pull-ups.
    DDRF &= ~(_BV(PF0) | _BV(PF1));
    PORTF &= ~(_BV(PF0) | _BV(PF1));
    DIDR0 |= _BV(ADC0D) | _BV(ADC1D);
    ADCSRA = _BV(ADEN) | _BV(ADPS2) | _BV(ADPS1); // 125 kHz ADC.
    joystick.setXAxisRange(0, 1023);
    joystick.setYAxisRange(0, 1023);
    joystick.begin(false);
}

void loop() {
    static uint32_t previous = 0;
    const uint32_t now = millis();
    if (uint32_t(now - previous) < 2) return;
    previous = now;
    const uint8_t raw = uint8_t(~PIND) & 0x1f;
    for (uint8_t i = 0; i < 5; ++i) {
        // Saturating integrator: a stable change takes five 2 ms samples.
        if (raw & _BV(i)) {
            if (debounce[i] < 5) ++debounce[i];
        } else if (debounce[i] > 0) {
            --debounce[i];
        }
        if (debounce[i] == 5) pressed |= _BV(i);
        if (debounce[i] == 0) pressed &= ~_BV(i);
        joystick.setButton(i, bool(pressed & _BV(i)));
    }
    joystick.setXAxis(readAxis(0));
    joystick.setYAxis(readAxis(1));
    if (USBDevice.configured() && !USBDevice.isSuspended()) joystick.sendState();
    // Issue #5000: services a USB clock-restart request the WAKEUPI ISR
    // branch defers to main-loop context; see patches/apply_usbcore_patch.py.
    USBDevice.poll();
}
