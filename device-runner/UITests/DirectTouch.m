#import "DirectTouch.h"

#import <XCTest/XCTest.h>

// The private API, declared as protocols so nothing links against the private classes and ARC
// still sees `init…` as an initializer.
@protocol FDTPointerEventPath <NSObject>
- (instancetype)initForTouchAtPoint:(CGPoint)point offset:(double)offset;
- (void)liftUpAtOffset:(double)offset;
@end

@protocol FDTSynthesizedEventRecord <NSObject>
- (instancetype)initWithName:(NSString *)name interfaceOrientation:(NSInteger)orientation;
- (void)addPointerEventPath:(id)path;
@end

@protocol FDTEventSynthesizer <NSObject>
- (void)synthesizeEvent:(id)record completion:(void (^)(BOOL result, NSError *_Nullable error))completion;
@end

static id<FDTEventSynthesizer> _Nullable FDTSynthesizer(void) {
    XCUIDevice *device = XCUIDevice.sharedDevice;
    SEL selector = NSSelectorFromString(@"eventSynthesizer");
    if (![device respondsToSelector:selector]) {
        return nil;
    }
    id (*getter)(id, SEL) = (id (*)(id, SEL))[device methodForSelector:selector];
    id synthesizer = getter(device, selector);
    return [synthesizer respondsToSelector:@selector(synthesizeEvent:completion:)] ? synthesizer : nil;
}

BOOL FDTDirectTouchAvailable(void) {
    Class path = NSClassFromString(@"XCPointerEventPath");
    Class record = NSClassFromString(@"XCSynthesizedEventRecord");
    return path && record
        && [path instancesRespondToSelector:@selector(initForTouchAtPoint:offset:)]
        && [path instancesRespondToSelector:@selector(liftUpAtOffset:)]
        && [record instancesRespondToSelector:@selector(initWithName:interfaceOrientation:)]
        && [record instancesRespondToSelector:@selector(addPointerEventPath:)]
        && FDTSynthesizer() != nil;
}

NSString *_Nullable FDTDirectTap(CGPoint point, double hold, NSInteger orientation, double timeout) {
    if (!FDTDirectTouchAvailable()) {
        return @"this XCTest has no event record to send a touch with";
    }
    id<FDTPointerEventPath> path = [[NSClassFromString(@"XCPointerEventPath") alloc] initForTouchAtPoint:point offset:0];
    [path liftUpAtOffset:hold];
    id<FDTSynthesizedEventRecord> record =
        [[NSClassFromString(@"XCSynthesizedEventRecord") alloc] initWithName:@"fast tap" interfaceOrientation:orientation];
    [record addPointerEventPath:path];

    dispatch_semaphore_t delivered = dispatch_semaphore_create(0);
    __block BOOL accepted = NO;
    __block NSError *failure = nil;
    [FDTSynthesizer() synthesizeEvent:record completion:^(BOOL result, NSError *_Nullable error) {
        accepted = result;
        failure = error;
        dispatch_semaphore_signal(delivered);
    }];
    // The completion may come on the main queue: keep its run loop turning while waiting.
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    while (dispatch_semaphore_wait(delivered, DISPATCH_TIME_NOW) != 0) {
        if (deadline.timeIntervalSinceNow <= 0) {
            return @"the touch was sent and XCTest did not confirm it in time";
        }
        [NSRunLoop.currentRunLoop runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.005]];
    }
    if (failure) {
        return failure.localizedDescription;
    }
    return accepted ? nil : @"XCTest did not accept the touch";
}
