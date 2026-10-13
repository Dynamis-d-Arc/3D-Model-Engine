#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"

/**
 * Twin metadata lives in actor tags written by the importer (twin/unreal/scripts/import_twin.py):
 *   "Twin"                      marks every generated actor
 *   "TwinKind=floor|wall|ceiling|door_leaf|door_frame|window_frame|glass|slab|furniture|project"
 *   "TwinRoom=R1", "TwinRoomName=Living", "TwinDims=6.25 x 4.25 m", "TwinArea=26.6 m2",
 *   "TwinSpawn=x,y,z" (cm, room label point on the floor), "TwinLabel=3-seat sofa",
 *   "TwinAsset=sofa_3seat", "TwinSize=2.20 x 0.95 x 0.85 m", "TwinId=F1", "TwinName=<object name>"
 */
namespace TwinMeta
{
	inline FString Get(const AActor* Actor, const TCHAR* Key)
	{
		if (!Actor)
		{
			return FString();
		}
		const FString Prefix = FString::Printf(TEXT("Twin%s="), Key);
		for (const FName& Tag : Actor->Tags)
		{
			const FString S = Tag.ToString();
			if (S.StartsWith(Prefix, ESearchCase::CaseSensitive))
			{
				return S.RightChop(Prefix.Len());
			}
		}
		return FString();
	}

	inline bool IsKind(const AActor* Actor, const TCHAR* Kind)
	{
		return Get(Actor, TEXT("Kind")) == Kind;
	}

	inline bool IsTwin(const AActor* Actor)
	{
		return Actor && Actor->Tags.Contains(FName(TEXT("Twin")));
	}

	inline bool ParseVector(const FString& S, FVector& Out)
	{
		TArray<FString> Parts;
		S.ParseIntoArray(Parts, TEXT(","));
		if (Parts.Num() != 3)
		{
			return false;
		}
		Out = FVector(FCString::Atod(*Parts[0]), FCString::Atod(*Parts[1]), FCString::Atod(*Parts[2]));
		return true;
	}
}
