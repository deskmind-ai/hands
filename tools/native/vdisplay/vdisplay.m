// A virtual display for runs that should not use the user's screen: the gym's windows are put on it, where input
// is delivered in the background (drivers/skylight.py) and nothing covers or is covered by the user's work.
// CGVirtualDisplay is private API (the class DeskPad and BetterDisplay use); for internal tooling only.
//
//   vdisplay [width height]   -- prints "display <id> <x> <y> <w> <h>" once it exists, keeps it until killed
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>

@interface CGVirtualDisplayDescriptor : NSObject
@property(retain, nonatomic) dispatch_queue_t queue;
@property(retain, nonatomic) NSString *name;
@property(nonatomic) unsigned int maxPixelsHigh, maxPixelsWide, vendorID, productID, serialNum;
@property(nonatomic) CGSize sizeInMillimeters;
@property(copy, nonatomic) void (^terminationHandler)(id, id);
@end
@interface CGVirtualDisplayMode : NSObject
- (instancetype)initWithWidth:(unsigned int)w height:(unsigned int)h refreshRate:(double)r;
@end
@interface CGVirtualDisplaySettings : NSObject
@property(retain, nonatomic) NSArray *modes;
@property(nonatomic) unsigned int hiDPI;
@end
@interface CGVirtualDisplay : NSObject
@property(readonly, nonatomic) unsigned int displayID;
- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)d;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)s;
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        unsigned int w = argc > 2 ? (unsigned int)atoi(argv[1]) : 1920, h = argc > 2 ? (unsigned int)atoi(argv[2]) : 1200;
        CGVirtualDisplayDescriptor *d = [CGVirtualDisplayDescriptor new];
        d.queue = dispatch_get_main_queue();
        d.name = @"DeskMind Runs";
        d.maxPixelsWide = w; d.maxPixelsHigh = h;
        d.sizeInMillimeters = CGSizeMake(w * 25.4 / 110, h * 25.4 / 110);
        d.vendorID = 0x3456; d.productID = 0x1; d.serialNum = 0x1;
        d.terminationHandler = ^(id a, id b) { exit(0); };
        CGVirtualDisplay *display = [[CGVirtualDisplay alloc] initWithDescriptor:d];
        if (!display) { fprintf(stderr, "could not create the display\n"); return 1; }
        CGVirtualDisplaySettings *s = [CGVirtualDisplaySettings new];
        s.hiDPI = 0;
        s.modes = @[[[CGVirtualDisplayMode alloc] initWithWidth:w height:h refreshRate:30]];
        if (![display applySettings:s]) { fprintf(stderr, "could not apply the mode\n"); return 1; }
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            CGRect b = CGDisplayBounds(display.displayID);
            printf("display %u %.0f %.0f %.0f %.0f\n", display.displayID, b.origin.x, b.origin.y, b.size.width, b.size.height);
            fflush(stdout);
        });
        dispatch_main();
    }
}
