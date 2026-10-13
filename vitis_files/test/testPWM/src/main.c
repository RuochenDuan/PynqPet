#include "sleep.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xil_types.h"

#define PWM_BASEADDR  0x43C40000U
#define VIB_DUTY      0x00U
#define VIB_CTRL      0x04U
#define VIB_STATUS    0x08U

static u32 pwm_read(u32 offset)
{
    return Xil_In32(PWM_BASEADDR + offset);
}

static void pwm_write(u32 offset, u32 value)
{
    Xil_Out32(PWM_BASEADDR + offset, value);
}

static void pwm_set(u32 duty)
{
    duty &= 0xffU;

    pwm_write(VIB_DUTY, duty);
    pwm_write(VIB_CTRL, duty == 0U ? 0U : 1U);

    xil_printf("duty=%3lu  ctrl=0x%08lx  status=0x%08lx\r\n",
               (unsigned long)duty,
               (unsigned long)pwm_read(VIB_CTRL),
               (unsigned long)pwm_read(VIB_STATUS));
}

int main(void)
{
    const u32 duty_list[] = {
        64U,
        128U,
        205U,
        255U
    };
    unsigned int i;

    xil_printf("\r\nPWM vibration strength demo start\r\n");
    xil_printf("PWM pin U13 / Arduino D10 -> motor IN, VCC -> 3V3 or 5V, GND -> GND\r\n");
    xil_printf("The program shows weak, medium, strong, full once.\r\n");
    xil_printf("Each strength is separated by an off interval.\r\n\r\n");

    for (i = 0U; i < sizeof(duty_list) / sizeof(duty_list[0]); i++) {
        pwm_set(0U);
        sleep(1);
        pwm_set(duty_list[i]);
        sleep(3);
    }

    pwm_set(0U);
    xil_printf("\r\nPWM vibration strength demo done\r\n");

    return 0;
}
