#!/usr/bin/env python3
"""
Two modes:
  python build_ios.py test     -> builds a tiny UIKit app that shows a GREEN screen.
                                  If this IPA also shows black, the problem is packaging/signing.
                                  If it shows green, your pipeline is fine and the black screen
                                  is just missing Saturn code.
  python build_ios.py saturn   -> tries to compile the real Saturn sources, logs every error,
                                  and only makes an IPA if it all compiled and links with a main().
"""
import glob, os, plistlib, re, shutil, subprocess, sys, zipfile
from concurrent.futures import ThreadPoolExecutor

MIN_IOS = "14.0"
STAGING = "Saturn_Build_Staging"

def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

def sdk_path():
    return subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"]).decode().strip()

def write_plist(app_dir, exe, bundle_id, name):
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": exe,
        "CFBundleIdentifier": bundle_id,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "MinimumOSVersion": MIN_IOS,
        "LSRequiresIPhoneOS": True,
        "UIDeviceFamily": [1, 2],
        "UIRequiredDeviceCapabilities": ["arm64"],
        # Without a launch screen key iOS runs the app in a letterboxed legacy mode (black bars).
        "UILaunchScreen": {},
        "UIRequiresFullScreen": True,
        "UISupportedInterfaceOrientations": [
            "UIInterfaceOrientationLandscapeLeft", "UIInterfaceOrientationLandscapeRight",
            "UIInterfaceOrientationPortrait"],
        "UISupportedInterfaceOrientations~ipad": [
            "UIInterfaceOrientationLandscapeLeft", "UIInterfaceOrientationLandscapeRight",
            "UIInterfaceOrientationPortrait", "UIInterfaceOrientationPortraitUpsideDown"],
    }
    with open(os.path.join(app_dir, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

def make_ipa(app_dir, ipa_name):
    payload_root = os.path.join(STAGING, "ipa_root")
    if os.path.exists(payload_root):
        shutil.rmtree(payload_root)
    payload = os.path.join(payload_root, "Payload")
    os.makedirs(payload)
    shutil.copytree(app_dir, os.path.join(payload, os.path.basename(app_dir)))
    if os.path.exists(ipa_name):
        os.remove(ipa_name)
    with zipfile.ZipFile(ipa_name, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(payload):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, payload_root).replace("\\", "/"))
    print(f"[OK] wrote {ipa_name}")

TEST_MAIN = r'''
#import <UIKit/UIKit.h>
@interface AppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end
@implementation AppDelegate
- (BOOL)application:(UIApplication *)a didFinishLaunchingWithOptions:(NSDictionary *)o {
    self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
    UIViewController *vc = [UIViewController new];
    vc.view.backgroundColor = [UIColor colorWithRed:0.1 green:0.6 blue:0.2 alpha:1];
    UILabel *l = [[UILabel alloc] initWithFrame:vc.view.bounds];
    l.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    l.text = @"IPA pipeline works";
    l.textAlignment = NSTextAlignmentCenter;
    l.textColor = UIColor.whiteColor;
    l.font = [UIFont boldSystemFontOfSize:32];
    [vc.view addSubview:l];
    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];
    return YES;
}
@end
int main(int argc, char *argv[]) {
    @autoreleasepool { return UIApplicationMain(argc, argv, nil, NSStringFromClass([AppDelegate class])); }
}
'''

def build_test():
    sdk = sdk_path()
    app = os.path.join(STAGING, "SaturnTest.app")
    shutil.rmtree(STAGING, ignore_errors=True)
    os.makedirs(app)
    src = os.path.join(STAGING, "main.m")
    open(src, "w").write(TEST_MAIN)
    r = sh(["clang", "-fobjc-arc", "-arch", "arm64", "-isysroot", sdk,
            f"-miphoneos-version-min={MIN_IOS}", "-framework", "UIKit", "-framework", "Foundation",
            src, "-o", os.path.join(app, "SaturnTest")])
    if r.returncode:
        print(r.stderr); sys.exit(1)
    write_plist(app, "SaturnTest", "com.zezo.saturntest", "Saturn Test")
    make_ipa(app, "SaturnTest.ipa")

EXCLUDE = re.compile(r"(direct3d|dxgi|gfx_glx|gfx_sdl1|audio_alsa|audio_pulse|audio_wasapi|"
                     r"discord|/tools/|/n64/|ultra|/build/|Saturn_Build_Staging|/\.git)")

def build_saturn():
    sdk = sdk_path()
    root = "Saturn" if os.path.isdir("Saturn") else "."
    srcs = [f for f in glob.glob(f"{root}/**/*.c", recursive=True) + glob.glob(f"{root}/**/*.cpp", recursive=True)
            if not EXCLUDE.search(f)]
    print(f"[+] {len(srcs)} source files under {root}")
    if not srcs:
        print("[!] No Saturn sources found. The repo needs the real Saturn code (see workflow checkout)."); sys.exit(1)
    inc = sorted({os.path.dirname(h) for h in glob.glob(f"{root}/**/*.h", recursive=True) if not EXCLUDE.search(h)})
    inc_flags = [x for d in inc for x in ("-I", d)]
    objdir = os.path.join(STAGING, "obj")
    shutil.rmtree(STAGING, ignore_errors=True)
    os.makedirs(objdir)
    defs = ["-DTARGET_IOS=1", "-DTARGET_PC=1", "-DVERSION_US=1", "-DNO_SEGMENTED_MEMORY=1", "-DUSE_GLES=1"]

    def compile_one(i_f):
        i, f = i_f
        obj = os.path.join(objdir, f"{i}.o")
        cc = ["clang++", "-std=c++17"] if f.endswith(".cpp") else ["clang"]
        r = sh(cc + ["-arch", "arm64", "-isysroot", sdk, f"-miphoneos-version-min={MIN_IOS}",
                     "-c", "-w"] + defs + inc_flags + [f, "-o", obj])
        return f, obj, r.returncode, r.stderr

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        results = list(ex.map(compile_one, enumerate(srcs)))
    failed = [r for r in results if r[2]]
    with open("compile_errors.log", "w") as log:
        for f, _, _, err in failed:
            log.write(f"===== {f}\n{err}\n")
    print(f"[+] compiled OK: {len(results) - len(failed)}  failed: {len(failed)}  (see compile_errors.log)")
    if failed:
        sys.exit(1)

    app = os.path.join(STAGING, "Saturn.app")
    os.makedirs(app)
    exe = os.path.join(app, "Saturn")
    r = sh(["clang++", "-arch", "arm64", "-isysroot", sdk, f"-miphoneos-version-min={MIN_IOS}"]
           + [o for _, o, _, _ in results]
           + ["-framework", "UIKit", "-framework", "Foundation", "-framework", "OpenGLES",
              "-framework", "CoreGraphics", "-framework", "QuartzCore", "-o", exe])
    if r.returncode:
        open("link_errors.log", "w").write(r.stderr)
        print("[!] link failed, see link_errors.log"); sys.exit(1)
    syms = sh(["nm", "-gU", exe]).stdout
    if "_main" not in syms:
        print("[!] No main() in the binary: it would launch to a black screen. Not packaging."); sys.exit(1)
    write_plist(app, "Saturn", "com.zezo.saturnmobile", "Saturn")
    make_ipa(app, "SaturnCompletePackage.ipa")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    {"test": build_test, "saturn": build_saturn}[mode]()
