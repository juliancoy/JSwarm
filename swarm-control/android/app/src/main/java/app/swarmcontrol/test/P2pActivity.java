package app.swarmcontrol.test;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

/** Returns to CompAI's paired browser and narrowly scoped test-delivery channel. */
public class P2pActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://compai.lifetech.fyi/swarm-control")));
        finish();
    }
}
