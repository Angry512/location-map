"""Build the portable, unsigned Location Map workflow for Apple signing."""
from pathlib import Path
import argparse
import plistlib
from uuid import NAMESPACE_URL, uuid5


ASHELL = 'AsheKube.app.a-Shell'
VPN_BUNDLE = 'com.jkcoxson.LocalDevVPN'
VPN_INTENT = 'ControlLocalDevVPNIntent'
SEED = 'location-map-seed.txt'


def reference(name):
    return str(uuid5(NAMESPACE_URL, 'urn:location-map:shortcut:' + name)).upper()


def output(name, *, text=False):
    value = {'Type': 'ActionOutput', 'OutputName': name, 'OutputUUID': reference(name)}
    if text:
        value['Aggrandizements'] = [{'Type': 'WFCoercionVariableAggrandizement',
                                    'CoercionItemClass': 'WFStringContentItem'}]
    return {'WFSerializationType': 'WFTextTokenAttachment', 'Value': value}


def action(identifier, **parameters):
    return {'WFWorkflowActionIdentifier': identifier,
            'WFWorkflowActionParameters': parameters}


def workflow():
    group = reference('No Wi-Fi')
    condition = 'is.workflow.actions.conditional'
    cellular = 'is.workflow.actions.cellulardata.set'
    return {
        'WFWorkflowName': 'Location Map',
        'WFWorkflowClientVersion': '5037.109',
        'WFWorkflowMinimumClientVersion': 900,
        'WFWorkflowMinimumClientVersionString': '900',
        'WFWorkflowIcon': {'WFWorkflowIconStartColor': 1440408063,
                           'WFWorkflowIconGlyphNumber': 61444},
        'WFWorkflowTypes': ['WFWorkflowTypeShowInSearch'],
        'WFQuickActionSurfaces': [],
        'WFWorkflowInputContentItemClasses': [],
        'WFWorkflowOutputContentItemClasses': [],
        'WFWorkflowHasOutputFallback': False,
        'WFWorkflowHasShortcutInputVariables': False,
        'WFWorkflowImportQuestions': [],
        'WFWorkflowActions': [
            # LocalDevVPN defaults to Enable and keeps its own local profile.
            action(VPN_BUNDLE + '.' + VPN_INTENT, UUID=reference('Enable LocalDevVPN'),
                   AppIntentDescriptor={'TeamIdentifier': '42Q7QX86GV', 'BundleIdentifier': VPN_BUNDLE,
                                        'Name': 'LocalDevVPN', 'AppIntentIdentifier': VPN_INTENT}),
            action('is.workflow.actions.getcurrentlocation', UUID=reference('Current Location')),
            action('is.workflow.actions.getmapslink', WFInput=output('Current Location'),
                   UUID=reference('Maps URL')),
            action('is.workflow.actions.setitemname', WFInput=output('Maps URL', text=True),
                   WFName=SEED, UUID=reference('Renamed Item')),
            action(ASHELL + '.PutFileIntent', file=output('Renamed Item'),
                   overwrite=True, ShowWhenRun=False),
            action('is.workflow.actions.openapp', WFAppIdentifier=ASHELL,
                   WFSelectedApp={'BundleIdentifier': ASHELL, 'Name': 'a-Shell',
                                  'TeamIdentifier': 'VG8Z23C8YL'}),
            action('is.workflow.actions.getwifi', WFNetworkDetailsNetwork='Wi-Fi',
                   WFWiFiDetail='Network Name', UUID=reference('Network Details')),
            action(condition, GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=101,
                   WFInput={'Type': 'Variable', 'Variable': output('Network Details')}),
            action(cellular, operation='toggle'),
            action('is.workflow.actions.delay', WFDelayTime=8),
            action(cellular, operation='toggle'),
            action(condition, GroupingIdentifier=group, WFControlFlowMode=1),
            action(condition, GroupingIdentifier=group, WFControlFlowMode=2),
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='destination .plist (unsigned)')
    args = parser.parse_args()
    if args.output.suffix.lower() != '.plist':
        parser.error('Use a .plist extension for the unsigned workflow')
    args.output.write_bytes(plistlib.dumps(workflow(), fmt=plistlib.FMT_XML))


if __name__ == '__main__':
    main()
