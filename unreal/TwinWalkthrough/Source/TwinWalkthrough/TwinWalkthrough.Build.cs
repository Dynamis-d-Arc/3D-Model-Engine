using UnrealBuildTool;

public class TwinWalkthrough : ModuleRules
{
	public TwinWalkthrough(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core", "CoreUObject", "Engine", "InputCore", "NavigationSystem", "AIModule", "Json", "JsonUtilities"
		});
		if (Target.bBuildEditor)
		{
			// TwinEditorUtils (WITH_EDITOR only): brush building for script-spawned volumes
			PrivateDependencyModuleNames.Add("UnrealEd");
		}
	}
}
