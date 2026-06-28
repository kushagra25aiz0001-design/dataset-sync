/*
 * ESP32 CSI Receiver — app_main.c
 *
 * This firmware:
 *   1. Initialises Wi-Fi in STA mode on the SAME channel as the transmitter.
 *   2. Enables promiscuous mode so it can receive ESP-NOW frames from an
 *      unassociated peer (the transmitter).
 *   3. Registers a CSI callback that fires once per received frame.
 *   4. The callback prints one CSV line to UART for every frame received:
 *
 *        timestamp_ms,seq,rssi,len,amp[0],amp[1],...,amp[N-1]
 *
 *      where N = number of CSI subcarriers (64 for HT20, 128 for HT40).
 *
 *   5. The PC-side Python script (collect_data.py) reads this stream and
 *      saves it to disk for LSTM training.
 *
 * Target  : ESP32-WROOM-32 (any ESP32 module)
 * IDF     : >= 4.4.1
 *
 * Build & Flash:
 *   idf.py set-target esp32
 *   idf.py build
 *   idf.py flash -b 921600 -p /dev/ttyUSB1 monitor
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

/* ─── Configuration ──────────────────────────────────────────────────────────
 * MUST match the transmitter's settings exactly.
 * ───────────────────────────────────────────────────────────────────────────
 */

/* Wi-Fi channel — must match transmitter (default: 6). */
#define CONFIG_LESS_INTERFERENCE_CHANNEL 6

/* Bandwidth — HT20 gives 64 subcarriers; HT40 gives 128.
 * Both sides MUST match.                                 */
#define CONFIG_WIFI_BANDWIDTH WIFI_BW_HT20

/* Transmitter's custom MAC (set in csi_transmitter/main/app_main.c).
 * The CSI filter uses this to ignore frames from other devices.      */
static const uint8_t TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};

/* ─── End Configuration ──────────────────────────────────────────────────── */

static const char *TAG = "csi_recv";

/* Sequence counter — incremented every time the CSI callback fires. */
static volatile uint32_t g_seq = 0;

/* ─────────────────────────────────────────────────────────────────────────
 * CSI Callback
 *
 * Called by the Wi-Fi driver from its internal task for EVERY received
 * frame that carries CSI data.  Keep it fast — heavy work causes drops.
 *
 * Output format (one line per packet):
 *   timestamp_ms,seq,rssi,csi_len,amp[0],...,amp[N-1]
 *
 * amp[i] = sqrt(I[i]^2 + Q[i]^2)  — amplitude of subcarrier i
 * ───────────────────────────────────────────────────────────────────────── */
static void csi_callback(void *ctx, wifi_csi_info_t *info) {
  /* ── Filter: only process frames from our transmitter ── */
  if (memcmp(info->mac, TX_MAC, 6) != 0) {
    return;
  }

  uint32_t seq = g_seq++;
  int64_t ts_ms = (int64_t)(xTaskGetTickCount()) * portTICK_PERIOD_MS;
  int8_t rssi = info->rx_ctrl.rssi;
  uint16_t csi_len = info->len; /* number of int8_t elements (I,Q pairs) */

  /* csi_len int8_t values = csi_len/2 complex samples.
   * Each sample: buf[2k] = I,  buf[2k+1] = Q  (signed 8-bit).            */
  int8_t *buf = info->buf;
  uint16_t n_carriers = csi_len / 2; /* number of subcarrier amplitudes */

  /* ── Print CSV header once at startup ── */
  static bool header_printed = false;
  if (!header_printed) {
    printf("# timestamp_ms,seq,rssi,n_carriers");
    for (uint16_t i = 0; i < n_carriers; i++) {
      printf(",amp%u", i);
    }
    printf("\n");
    header_printed = true;
  }

  /* ── Print CSV data row ── */
  printf("%lld,%lu,%d,%u", ts_ms, (unsigned long)seq, rssi, n_carriers);
  for (uint16_t i = 0; i < n_carriers; i++) {
    int8_t I = buf[2 * i];
    int8_t Q = buf[2 * i + 1];
    /* True L2 amplitude: sqrt(I²+Q²). ESP32 FPU executes sqrtf in ~1 cycle. */
    uint16_t amp = (uint16_t)sqrtf((float)I * I + (float)Q * Q);
    printf(",%u", amp);
  }
  printf("\n");
}

/* ─────────────────────────────────────────────────────────────────────────
 * Wi-Fi Init
 * ───────────────────────────────────────────────────────────────────────── */
static void wifi_init(void) {
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  ESP_ERROR_CHECK(esp_netif_init());

  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));

  /* Station mode — we don't connect to any AP. */
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

  /* Set bandwidth BEFORE start. */
  ESP_ERROR_CHECK(
      esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));

  ESP_ERROR_CHECK(esp_wifi_start());

  /* Disable power save — we need to receive every single packet. */
  ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

  /* Lock the channel — must match the transmitter. */
  if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                         WIFI_SECOND_CHAN_NONE));
  } else {
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL,
                                         WIFI_SECOND_CHAN_BELOW));
  }

  /* ── Promiscuous mode: required to receive CSI for non-associated peers ── */
  ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

  /* ── Register CSI callback ── */
  wifi_csi_config_t csi_cfg = {
      .lltf_en = true,            /* Legacy Long Training Field    */
      .htltf_en = true,           /* HT Long Training Field        */
      .stbc_htltf2_en = true,     /* STBC HT LTF2                  */
      .ltf_merge_en = true,       /* merge all LTFs into one report */
      .channel_filter_en = false, /* raw, unfiltered CSI           */
      .manu_scale = false,
  };
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));

  ESP_LOGI(TAG, "Wi-Fi + CSI init done. channel=%d  BW=%s",
           CONFIG_LESS_INTERFERENCE_CHANNEL,
           (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) ? "HT20" : "HT40");
}

/* ─────────────────────────────────────────────────────────────────────────
 * ESP-NOW Init
 *
 * We only need to initialise ESP-NOW so the stack accepts the incoming
 * ESP-NOW frames. We register the transmitter as a peer so its frames
 * are not silently dropped.
 * ───────────────────────────────────────────────────────────────────────── */
static void espnow_init(void) {
  ESP_ERROR_CHECK(esp_now_init());
  ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

  esp_now_peer_info_t peer = {
      .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
      .ifidx = WIFI_IF_STA,
      .encrypt = false,
  };
  memcpy(peer.peer_addr, TX_MAC, 6);
  ESP_ERROR_CHECK(esp_now_add_peer(&peer));

  ESP_LOGI(TAG, "ESP-NOW init done. Listening for TX MAC: " MACSTR,
           MAC2STR(TX_MAC));
}

/* ─────────────────────────────────────────────────────────────────────────
 * app_main
 * ───────────────────────────────────────────────────────────────────────── */
void app_main(void) {
  /* 1. NVS */
  esp_err_t ret = nvs_flash_init();
  if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
      ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    ret = nvs_flash_init();
  }
  ESP_ERROR_CHECK(ret);

  /* 2. Wi-Fi + CSI */
  wifi_init();

  /* 3. ESP-NOW */
  espnow_init();

  /* ── Ready banner ── */
  ESP_LOGI(TAG, "=========================================");
  ESP_LOGI(TAG, "  CSI RECEIVER READY");
  ESP_LOGI(TAG, "  Waiting for MAC: " MACSTR, MAC2STR(TX_MAC));
  ESP_LOGI(TAG, "  Channel: %d  BW: %s", CONFIG_LESS_INTERFERENCE_CHANNEL,
           (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) ? "HT20" : "HT40");
  ESP_LOGI(TAG, "  CSV rows will stream below...");
  ESP_LOGI(TAG, "=========================================");

  /*
   * Nothing left to do in app_main — the CSI callback fires automatically
   * for each received frame.  Just keep the task alive.
   */
  while (1) {
    /* Log a heartbeat every 10 s so you know the device is still alive. */
    vTaskDelay(pdMS_TO_TICKS(10000));
    ESP_LOGI(TAG, "[alive] packets_received=%lu  free_heap=%lu",
             (unsigned long)g_seq, (unsigned long)esp_get_free_heap_size());
  }
}
