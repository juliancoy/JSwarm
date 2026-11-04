package app.swarmcontrol.test;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import java.util.ArrayList;

/** A test launcher using the normal browser's cookies, never a separate WebView. */
public class MainActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        Intent browser = new Intent(Intent.ACTION_VIEW, Uri.parse("https://compai.lifetech.fyi/"));
        Bundle extras = new Bundle();
        extras.putBinder("android.support.customtabs.extra.SESSION", null);
        extras.putInt("android.support.customtabs.extra.TOOLBAR_COLOR", 0xff0b1417);
        Bundle item = new Bundle();
        item.putString("android.support.customtabs.customaction.MENU_ITEM_TITLE", "Swarm Control P2P connection");
        item.putParcelable("android.support.customtabs.customaction.PENDING_INTENT",
            PendingIntent.getActivity(this, 1, new Intent(this, P2pActivity.class), PendingIntent.FLAG_IMMUTABLE));
        ArrayList<Bundle> menu = new ArrayList<>(); menu.add(item);
        extras.putParcelableArrayList("android.support.customtabs.extra.MENU_ITEMS", menu);
        browser.putExtras(extras);
        startActivity(browser);
        finish();
    }
}
