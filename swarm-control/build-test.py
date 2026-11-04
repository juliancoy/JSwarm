#!/usr/bin/env python3
"""Build and publish the fixed Swarm Control APK to the CompAI console."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

root=Path(__file__).resolve().parent
project=root/'android'
sdk=Path(os.environ.get('ANDROID_HOME') or Path.home()/'.cache/orgportal-android-sdk')
if not (sdk/'platforms/android-36/android.jar').exists():
    raise SystemExit('Set ANDROID_HOME to an Android SDK with API 36 and build-tools 36.0.0')
jdk=Path(os.environ.get('SWARM_JAVA_HOME') or '/usr/lib/jvm/java-21-openjdk-amd64')
if not (jdk/'bin/javac').exists():jdk=Path.home()/'.cache/orgportal-jdk21/runtime'
if not (jdk/'bin/javac').exists():raise SystemExit('Set SWARM_JAVA_HOME to a JDK 21 installation')
subprocess.run([str(project/'gradlew'),'-p',str(project),'--offline','--no-daemon','assembleDebug'],env={**os.environ,'ANDROID_HOME':str(sdk),'JAVA_HOME':str(jdk)},check=True)
apk=project/'app/build/outputs/apk/debug/app-debug.apk'
subprocess.run([str(sdk/'build-tools/36.0.0/apksigner'),'verify',str(apk)],check=True)
output=root.parent/'.runtime/mobile'
output.mkdir(parents=True,exist_ok=True)
temporary=output/'swarm-control-test.apk.tmp'
shutil.copyfile(apk,temporary)
temporary.replace(output/'swarm-control-test.apk')
result={'package':'app.swarmcontrol.test','version':'0.1.0-test','size':apk.stat().st_size,'sha256':hashlib.sha256(apk.read_bytes()).hexdigest(),'console':'https://compai.lifetech.fyi/swarm-control','installedOnDevice':False}
(output/'swarm-control-test.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
