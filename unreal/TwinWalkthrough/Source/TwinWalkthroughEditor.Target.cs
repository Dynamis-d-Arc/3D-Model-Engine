using UnrealBuildTool;

public class TwinWalkthroughEditorTarget : TargetRules
{
	public TwinWalkthroughEditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("TwinWalkthrough");
	}
}
