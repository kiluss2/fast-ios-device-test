#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

// XCUICoordinate.tap() first resolves the app the coordinate is relative to (a snapshot over the
// USB link) and only then sends the touch: about 0.9 s per tap on an iPhone 15 Pro (iOS 18.5).
// WebDriverAgent sends the touch itself as XCTest's private event record. The classes and
// selectors are looked up at runtime, so an Xcode without them keeps the XCUITest tap.

/// Whether this XCTest has the event record, the pointer path and the device's event synthesizer.
BOOL FDTDirectTouchAvailable(void);

/// One finger down at `point` (screen points, in the UIInterfaceOrientation `orientation`) and up
/// `hold` seconds later, sent straight to XCTest's event synthesizer. Returns nil once XCTest
/// reports the touch delivered, else why not. Never sends the touch twice.
NSString *_Nullable FDTDirectTap(CGPoint point, double hold, NSInteger orientation, double timeout);

NS_ASSUME_NONNULL_END
