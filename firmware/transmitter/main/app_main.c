/*
 * ESP32 CSI Transmitter - app_main.c
 *
 * Based on Espressif's esp-csi example (csi_send):
 *   https://github.com/espressif/esp-csi/tree/master/examples/get-started/csi_send
 *
 * Reference project: nickbild/csi_hr (Heart Rate via WiFi CSI)
 *   https://github.com/nickbild/csi_hr
 *
 * This firmware configures the ESP32 as a Wi-Fi STA (no AP required),
 * then continuously broadcasts small ESP-NOW frames so the RECEIVER ESP32
 * on the other side can collect Channel State Information (CSI) data.
 *
 * Target: ESP32-WROOM-32 / ESP32-DevKitC / Adafruit HUZZAH32 (any ESP32)
 * IDF:    >= 4.4.1
 *
 * Build:
 *   idf.py set-target esp32
 *   idf.py build
 *   idf.py flash -b 921600 -p /dev/ttyUSB0 monitor
 */

#include <string.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

/* ─── Configuration ──────────────────────────────────────────────────────────
 * Adjust these values to match your environment.
 * They can also be overridden via  idf.py menuconfig  if you add a Kconfig.
 * ───────────────────────────────────────────────────────────────────────────
 */

/* Wi-Fi channel to use (1-13). Use a channel with low interference.
 * Both transmitter AND receiver must use the SAME channel.          */
#define CONFIG_LESS_INTERFERENCE_CHANNEL 6

/* Packets sent per second. 100 Hz is a good starting point for
 * heart-rate detection (matches the Pulse-Fi paper's data rate).   */
#define CONFIG_SEND_FREQUENCY 100

/* Wi-Fi bandwidth: WIFI_BW_HT20 (20 MHz) or WIFI_BW_HT40 (40 MHz).
 * HT40 yields 128 CSI subcarriers; HT20 yields 64.
 * Both sides must match.                                            */
#define CONFIG_WIFI_BANDWIDTH WIFI_BW_HT20

/* ESP-NOW physical layer mode. WIFI_PHY_MODE_HT20 is safest for
 * ESP32-WROOM-32.                                                   */
#define CONFIG_ESP_NOW_PHYMODE WIFI_PHY_MODE_HT20

/* ESP-NOW data rate. MCS0 (6.5 Mb/s with HT20) is standard.       */
#define CONFIG_ESP_NOW_RATE WIFI_PHY_RATE_MCS0_SGI

/* MAC address the transmitter will use.
 * The receiver's CSI filter uses this address to select frames.    */
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00,
                                              0x00, 0x00, 0x00};

/* ─── End Configuration ──────────────────────────────────────────────────── */

static const char *TAG = "csi_send";

/* ── 1. Wi-Fi Initialisation ─────────────────────────────────────────────── */
static void wifi_init(void) {
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  ESP_ERROR_CHECK(esp_netif_init());

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));

  /* Station mode – we never connect to an AP, but STA mode is needed
   * for ESP-NOW to work correctly.                                     */
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

  /* RAM storage – no need to save Wi-Fi credentials to flash.         */
  ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

  /* Bandwidth must be set BEFORE esp_wifi_start().                    */
  ESP_ERROR_CHECK(
      esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));

  ESP_ERROR_CHECK(esp_wifi_start());

  /* Disable power-save mode for lowest latency / most reliable tx.   */
  ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

  /* Lock onto the chosen channel.                                     */
  if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                         WIFI_SECOND_CHAN_NONE));
  } else {
    /* HT40: primary + secondary channel (below = ch-4).            */
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                         WIFI_SECOND_CHAN_BELOW));
  }

  /* Override the factory MAC with our fixed transmitter address so the
   * receiver can filter exactly our frames.                           */
  ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

/* ── 2. ESP-NOW Initialisation ───────────────────────────────────────────── */
static void wifi_esp_now_init(esp_now_peer_info_t peer) {
  ESP_ERROR_CHECK(esp_now_init());

  /* PMK = Primary Master Key (optional but recommended).             */
  ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

  /* Register broadcast peer (FF:FF:FF:FF:FF:FF).                    */
  ESP_ERROR_CHECK(esp_now_add_peer(&peer));

  /* Set PHY mode and data rate for this peer.                        */
  esp_now_rate_config_t rate_config = {
      .phymode = CONFIG_ESP_NOW_PHYMODE,
      .rate = CONFIG_ESP_NOW_RATE,
      .ersu = false,
      /* .dcm not present in IDF v5.1.x — added in later versions */
  };
  ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

/* ── 3. Application Entry Point ─────────────────────────────────────────── */
void app_main(void) {
  /* --- NVS (Non-Volatile Storage) init --- */
  esp_err_t ret = nvs_flash_init();
  if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
      ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    ret = nvs_flash_init();
  }
  ESP_ERROR_CHECK(ret);

  /* --- Wi-Fi --- */
  wifi_init();

  /* --- ESP-NOW broadcast peer --- */
  esp_now_peer_info_t peer = {
      .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
      .ifidx = WIFI_IF_STA,
      .encrypt = false,
      .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff}, /* broadcast */
  };
  wifi_esp_now_init(peer);

  ESP_LOGI(TAG, "================ CSI SEND ================");
  ESP_LOGI(TAG, "channel: %d  frequency: %d Hz  mac: " MACSTR,
           CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY,
           MAC2STR(CONFIG_CSI_SEND_MAC));

  /*
   * Main loop: send an incrementing 32-bit counter as the payload.
   * The data itself is irrelevant – the receiver only needs the
   * Wi-Fi frame to land so it can measure the channel response (CSI).
   *
   * Sleep between sends: 1 000 000 µs / frequency
   *   e.g. 100 Hz → sleep 10 000 µs between packets
   */
  for (uint32_t count = 0;; ++count) {
    ret = esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
    if (ret != ESP_OK) {
      ESP_LOGW(TAG, "[tx #%lu] free_heap: %lu  error: %s", (unsigned long)count,
               (unsigned long)esp_get_free_heap_size(), esp_err_to_name(ret));
    }

    usleep(1000000 / CONFIG_SEND_FREQUENCY);
  }
}
