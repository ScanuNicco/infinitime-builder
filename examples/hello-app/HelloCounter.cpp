#include "HelloCounter.h"
#include "components/motor/MotorController.h"
#include "displayapp/InfiniTimeTheme.h"

using namespace Pinetime::Applications::Screens;

extern lv_font_t jetbrains_mono_76;

HelloCounter::HelloCounter(Controllers::MotorController& motor) : motor {motor} {
  lv_obj_t* title = lv_label_create(lv_scr_act(), nullptr);
  lv_label_set_text_static(title, "Hello!\nTap to count");
  lv_label_set_align(title, LV_LABEL_ALIGN_CENTER);
  lv_obj_align(title, nullptr, LV_ALIGN_IN_TOP_MID, 0, 20);

  countLabel = lv_label_create(lv_scr_act(), nullptr);
  lv_obj_set_style_local_text_font(countLabel, LV_LABEL_PART_MAIN, LV_STATE_DEFAULT, &jetbrains_mono_76);
  lv_label_set_text_static(countLabel, "0");
  lv_obj_align(countLabel, nullptr, LV_ALIGN_CENTER, 0, 30);
}

HelloCounter::~HelloCounter() {
  lv_obj_clean(lv_scr_act());
}

bool HelloCounter::OnTouchEvent(TouchEvents event) {
  if (event != TouchEvents::Tap) {
    return false;  // let swipes etc. fall through to the system
  }
  count++;
  lv_label_set_text_fmt(countLabel, "%lu", static_cast<unsigned long>(count));
  lv_obj_align(countLabel, nullptr, LV_ALIGN_CENTER, 0, 30);
  motor.RunForDuration(15);
  return true;
}
