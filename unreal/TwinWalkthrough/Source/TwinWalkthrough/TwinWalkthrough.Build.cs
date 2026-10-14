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
		// runtime viewer (packaged game): backend API over HTTP, glTF meshes as procedural meshes,
		// PNG/JPEG textures, clipboard launch codes
		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"HTTP", "ProceduralMeshComponent", "ImageWrapper", "ApplicationCore",
			// streaming mode: a Slate input pre-processor reads the mouse look (see FTwinMouseDeltaProcessor)
			"Slate", "SlateCore"
		});
		if (Target.bBuildEditor)
		{
			// TwinEditorUtils (WITH_EDITOR only): brush building for script-spawned volumes
			PrivateDependencyModuleNames.Add("UnrealEd");
		}
	}
}
