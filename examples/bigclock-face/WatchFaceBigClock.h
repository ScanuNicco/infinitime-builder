#pragma once

#include <chrono>
#include <lvgl/lvgl.h>
#include "displayapp/apps/Apps.h"
#include "displayapp/screens/Screen.h"
#include "displayapp/Controllers.h"
#include "components/datetime/DateTimeController.h"
#include "utility/DirtyValue.h"

namespace Pinetime {
  namespace Applications {
    namespace Screens {
      class WatchFaceBigClock : public Screen {
      public:
        explicit WatchFaceBigClock(Controllers::DateTime& dateTime);
        ~WatchFaceBigClock() override;
        void Refresh() override;

      private:
        Controllers::DateTime& dateTime;
        Utility::DirtyValue<std::chrono::time_point<std::chrono::system_clock, std::chrono::minutes>> currentMinute {};
        lv_obj_t* timeLabel;
        lv_obj_t* dateLabel;
        lv_task_t* refreshTask;
      };
    }

    template <>
    struct WatchFaceTraits<WatchFace::BigClock> {
      static constexpr WatchFace watchFace = WatchFace::BigClock;
      static constexpr const char* name = "Big Clock";

      static Screens::Screen* Create(AppControllers& controllers) {
        return new Screens::WatchFaceBigClock(controllers.dateTimeController);
      }

      static bool IsAvailable(Pinetime::Controllers::FS& /*filesystem*/) {
        return true;
      }
    };
  }
}
