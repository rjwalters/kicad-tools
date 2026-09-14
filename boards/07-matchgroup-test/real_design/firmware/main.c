/* Board07 SDRAM exerciser. Built successfully does not imply hardware-tested.
 * Peripheral fields: ST RM0090; memory requirements: ISSI 42-45S16400J.
 * All application state/stack live in internal SRAM, never uninitialized SDRAM.
 */
#include "stm32f429xx.h"
#include <stdint.h>

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
static void fault(void) { for (;;) { __NOP(); } }
void Reset_Handler(void);
__attribute__((section(".isr_vector"), used))
const uintptr_t vectors[16] = {
    (uintptr_t)&_estack, (uintptr_t)Reset_Handler, (uintptr_t)fault, (uintptr_t)fault,
    (uintptr_t)fault, (uintptr_t)fault, (uintptr_t)fault, 0, 0, 0, 0,
    (uintptr_t)fault, (uintptr_t)fault, 0, (uintptr_t)fault, (uintptr_t)fault,
};

static void delay_ms(uint32_t ms) {
    while (ms--) {
        SysTick->LOAD = 96000U - 1U;
        SysTick->VAL = 0;
        SysTick->CTRL = SysTick_CTRL_CLKSOURCE_Msk | SysTick_CTRL_ENABLE_Msk;
        while (!(SysTick->CTRL & SysTick_CTRL_COUNTFLAG_Msk)) {}
        SysTick->CTRL = 0;
    }
}

static void clock_init(void) {
    RCC->CR |= RCC_CR_HSION;
    while (!(RCC->CR & RCC_CR_HSIRDY)) {}
    RCC->APB1ENR |= RCC_APB1ENR_PWREN;
    PWR->CR |= PWR_CR_VOS;
    FLASH->ACR = FLASH_ACR_ICEN | FLASH_ACR_DCEN | FLASH_ACR_PRFTEN | FLASH_ACR_LATENCY_3WS;
    /* HSI16 /16 *192 /2 =96MHz. APB1=24MHz, APB2=48MHz. */
    RCC->PLLCFGR = 16U | (192U << 6) | (4U << 24);
    RCC->CFGR = RCC_CFGR_PPRE1_DIV4 | RCC_CFGR_PPRE2_DIV2;
    RCC->CR |= RCC_CR_PLLON;
    while (!(RCC->CR & RCC_CR_PLLRDY)) {}
    RCC->CFGR |= RCC_CFGR_SW_PLL;
    while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_PLL) {}
    SCB->VTOR = 0x08000000U;
}

static void alternate(GPIO_TypeDef *port, unsigned pin, unsigned af) {
    port->MODER = (port->MODER & ~(3U << (2 * pin))) | (2U << (2 * pin));
    port->OSPEEDR |= 3U << (2 * pin);
    port->PUPDR &= ~(3U << (2 * pin));
    port->OTYPER &= ~(1U << pin);
    unsigned shift = (pin % 8) * 4;
    port->AFR[pin / 8] = (port->AFR[pin / 8] & ~(15U << shift)) | (af << shift);
}

static void uart_puts(const char *text) {
    while (*text) {
        while (!(USART2->SR & USART_SR_TXE)) {}
        USART2->DR = (uint8_t)*text++;
    }
}

static void uart_hex(uint32_t value) {
    const char digits[] = "0123456789ABCDEF";
    char text[9];
    for (unsigned i = 0; i < 8; ++i) text[i] = digits[(value >> (28 - i * 4)) & 15];
    text[8] = 0;
    uart_puts(text);
}

static void io_init(void) {
    RCC->AHB1ENR |= 0x7FU; /* GPIOA through GPIOG. */
    (void)RCC->AHB1ENR;
    alternate(GPIOA, 2, 7);
    alternate(GPIOA, 3, 7);
    RCC->APB1ENR |= RCC_APB1ENR_USART2EN;
    USART2->BRR = 208U; /* 24MHz/115200, oversampling16. */
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
    GPIOG->MODER = (GPIOG->MODER & ~((3U << 26) | (3U << 28))) | (1U << 26) | (1U << 28);
    GPIOG->BSRR = (1U << (13 + 16)) | (1U << (14 + 16));
}

static void command(uint32_t mode, uint32_t extra) {
    while (FMC_Bank5_6->SDSR & FMC_SDSR_BUSY) {}
    FMC_Bank5_6->SDCMR = mode | FMC_SDCMR_CTB2 | extra;
    while (FMC_Bank5_6->SDSR & FMC_SDSR_BUSY) {}
}

static void sdram_init(void) {
    enum { TMRD = 2, TXSR = 7, TRAS = 4, TRC = 7, TWR = 3, TRP = 2, TRCD = 2 };
    /* RM0090 FMC_SDTR: meet controller dependencies as well as memory minima. */
    _Static_assert(TWR >= TRAS - TRCD, "FMC write recovery versus active time");
    _Static_assert(TWR >= TRC - TRCD - TRP, "FMC write recovery versus row cycle");
    const uint16_t masks[6] = {
        (1U << 5) | (1U << 6), 1U << 0,
        (1U << 0) | (1U << 1) | (1U << 8) | (1U << 9) | (1U << 10) | (1U << 14) | (1U << 15),
        (1U << 0) | (1U << 1) | (0x1FFU << 7),
        0x3FU | (0x1FU << 11),
        (1U << 0) | (1U << 1) | (1U << 4) | (1U << 5) | (1U << 8) | (1U << 15),
    };
    GPIO_TypeDef *ports[] = {GPIOB, GPIOC, GPIOD, GPIOE, GPIOF, GPIOG};
    for (unsigned i = 0; i < 6; ++i)
        for (unsigned pin = 0; pin < 16; ++pin)
            if (masks[i] & (1U << pin)) alternate(ports[i], pin, 12);
    RCC->AHB3ENR |= RCC_AHB3ENR_FMCEN;
    (void)RCC->AHB3ENR;
    /* Bank2:8column/12row/16bit/4banks/CAS3; shared48MHz clock, burst/readpipe1. */
    uint32_t common = (2U << 10) | (1U << 12) | (1U << 13);
    FMC_Bank5_6->SDCR[0] = common;
    FMC_Bank5_6->SDCR[1] = common | (1U << 2) | (1U << 4) | (1U << 6) | (3U << 7);
    uint32_t timing = (TMRD - 1U) | ((TXSR - 1U) << 4) | ((TRAS - 1U) << 8)
        | ((TRC - 1U) << 12) | ((TWR - 1U) << 16) | ((TRP - 1U) << 20) | ((TRCD - 1U) << 24);
    FMC_Bank5_6->SDTR[0] = timing;
    FMC_Bank5_6->SDTR[1] = timing;
    command(1, 0); /* Clock enable. */
    delay_ms(1);
    command(2, 0); /* Precharge all. */
    command(3, 7U << 5); /* Eight auto refreshes. */
    command(4, 0x230U << 9); /* Burst1, sequential, CAS3, single write burst. */
    /* Conservative 680 count: under64ms/4096 even with HSI4% slow. */
    FMC_Bank5_6->SDRTR = 680U << 1;
    __DSB();
}

static uint32_t pattern(uint32_t index, unsigned pass) {
    if (pass == 0) return 0xAAAAAAAAU;
    if (pass == 1) return 0x55555555U;
    if (pass == 2) return index ^ 0x13579BDFU;
    return ~(index ^ 0x13579BDFU);
}

static void failed(uint32_t address, uint32_t expected, uint32_t actual) {
    GPIOG->BSRR = (1U << 14) | (1U << (13 + 16));
    uart_puts("FAIL address="); uart_hex(address);
    uart_puts(" expected="); uart_hex(expected);
    uart_puts(" actual="); uart_hex(actual); uart_puts("\r\n");
    fault();
}

static void byte_masks(void) {
    volatile uint32_t *word = (volatile uint32_t *)0xD0000000U;
    volatile uint8_t *bytes = (volatile uint8_t *)word;
    for (unsigned lane = 0; lane < 4; ++lane) {
        *word = 0x12345678U;
        __DSB();
        bytes[lane] = 0xA5U;
        __DSB();
        uint32_t expected = (0x12345678U & ~(0xFFU << (lane * 8))) | (0xA5U << (lane * 8));
        uint32_t actual = *word;
        if (actual != expected) failed((uint32_t)word, expected, actual);
    }
}

void Reset_Handler(void) {
    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) *dst++ = *src++;
    for (uint32_t *dst = &_sbss; dst < &_ebss;) *dst++ = 0;
    clock_init(); io_init();
    uart_puts("Board07 SDRAM test; HCLK96MHz SDCLK48MHz; 8MiB\r\n");
    sdram_init();
    volatile uint32_t *memory = (volatile uint32_t *)0xD0000000U;
    for (;;) {
        byte_masks(); /* Check LDQM/UDQM preserve the other byte of each16-bit write. */
        for (unsigned pass = 0; pass < 4; ++pass) {
            GPIOG->BSRR = 1U << (13 + 16);
            for (uint32_t i = 0; i < (8U * 1024 * 1024 / 4); ++i) memory[i] = pattern(i, pass);
            __DSB();
            delay_ms(100); /* Also exercise automatic refresh. */
            for (uint32_t i = 0; i < (8U * 1024 * 1024 / 4); ++i) {
                uint32_t actual = memory[i], expected = pattern(i, pass);
                if (actual != expected) failed(0xD0000000U + 4 * i, expected, actual);
            }
        }
        GPIOG->BSRR = 1U << 13;
        uart_puts("PASS 8MiB: byte masks, 4 patterns, refresh retention\r\n");
        delay_ms(1000);
    }
}
