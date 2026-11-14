#pragma once

#include "displayapp/apps/Apps.h"
#include "displayapp/screens/Screen.h"
#include "displayapp/Controllers.h"
#include <lvgl/lvgl.h>

namespace Pinetime {
  namespace Applications {
    namespace Screens {
      class HelloCounter : public Screen {
      public:
        explicit HelloCounter(Controllers::MotorController& motor);
        ~HelloCounter() override;
        bool OnTouchEvent(TouchEvents event) override;

      private:
        Controllers::MotorController& motor;
        lv_obj_t* countLabel;
        uint32_t count = 0;
      };
    }

    template <>
    struct AppTraits<Apps::HelloCounter> {
      static constexpr Apps app = Apps::HelloCounter;
      // FontAwesome "smile" (0xf118) - declared as "icon" in infinitime-app.json so the
      // builder adds the glyph to the launcher font.
      static constexpr const char* icon = "\xEF\x84\x98";

      static Screens::Screen* Create(AppControllers& controllers) {
        return new Screens::HelloCounter(controllers.motorController);
      }

      static bool IsAvailable(Pinetime::Controllers::FS& /*filesystem*/) {
        return true;
      }
    };
  }
}
