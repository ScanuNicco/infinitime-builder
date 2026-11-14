#include "WatchFaceBigClock.h"

using namespace Pinetime::Applications::Screens;

extern lv_font_t jetbrains_mono_76;

WatchFaceBigClock::WatchFaceBigClock(Controllers::DateTime& dateTime) : dateTime {dateTime} {
  timeLabel = lv_label_create(lv_scr_act(), nullptr);
  lv_obj_set_style_local_text_font(timeLabel, LV_LABEL_PART_MAIN, LV_STATE_DEFAULT, &jetbrains_mono_76);
  lv_obj_set_style_local_text_color(timeLabel, LV_LABEL_PART_MAIN, LV_STATE_DEFAULT, LV_COLOR_WHITE);

  dateLabel = lv_label_create(lv_scr_act(), nullptr);
  lv_obj_set_style_local_text_color(dateLabel, LV_LABEL_PART_MAIN, LV_STATE_DEFAULT, lv_color_hex(0x999999));

  refreshTask = lv_task_create(RefreshTaskCallback, 1000, LV_TASK_PRIO_MID, this);
  Refresh();
}

WatchFaceBigClock::~WatchFaceBigClock() {
  lv_task_del(refreshTask);
  lv_obj_clean(lv_scr_act());
}

void WatchFaceBigClock::Refresh() {
  currentMinute = std::chrono::time_point_cast<std::chrono::minutes>(dateTime.CurrentDateTime());
  if (currentMinute.IsUpdated()) {
    lv_label_set_text_fmt(timeLabel, "%02d:%02d", dateTime.Hours(), dateTime.Minutes());
    lv_obj_align(timeLabel, nullptr, LV_ALIGN_CENTER, 0, -20);
    lv_label_set_text_fmt(dateLabel, "%s %d", dateTime.DayOfWeekShortToString(), dateTime.Day());
    lv_obj_align(dateLabel, timeLabel, LV_ALIGN_OUT_BOTTOM_MID, 0, 10);
  }
}
