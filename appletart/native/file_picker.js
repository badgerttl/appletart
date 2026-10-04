// AppKit owns the panel directly; Standard Additions' background chooser is avoided.
ObjC.import("AppKit");

function run(argv) {
    const mode = argv[0];
    if (!["cloud", "iso", "directory"].includes(mode)) throw new Error("Invalid picker mode");
    const app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    const panel = $.NSOpenPanel.openPanel;
    panel.title = mode === "directory" ? "Select a local directory to share" :
        mode === "iso" ? "Select an ARM64 installer ISO" : "Select an ARM64 cloud image";
    panel.canChooseDirectories = mode === "directory";
    panel.canChooseFiles = mode !== "directory";
    panel.allowsMultipleSelection = false;
    if (mode !== "directory") panel.allowedFileTypes = mode === "iso" ? ["iso"] : ["qcow2", "raw", "img", "xz"];
    // Include the browser's Space, including a full-screen browser window.
    panel.collectionBehavior = $.NSWindowCollectionBehaviorCanJoinAllSpaces |
        $.NSWindowCollectionBehaviorFullScreenAuxiliary;
    panel.level = $.NSFloatingWindowLevel;
    panel.center;
    app.activateIgnoringOtherApps(true);
    panel.makeKeyAndOrderFront(null);
    panel.orderFrontRegardless;
    return panel.runModal === $.NSModalResponseOK ? ObjC.unwrap(panel.URL.path) : "";
}
